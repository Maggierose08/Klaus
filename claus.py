# restart test
# test comment
# restart worked

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import numpy as np
import pyaudio
import requests
import sounddevice as sd
import soundfile as sf
import speech_recognition as sr
from elevenlabs.client import ElevenLabs
import anthropic
from anthropic import Anthropic
from orb import set_status, set_level, request_restart

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
elevenlabs_client = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
recognizer = sr.Recognizer()
recognizer.pause_threshold = 1.5
mic = sr.Microphone()

ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")

WAKE_WORDS = ["hey claus", "yo claus", "claus", "klaus", "hey klaus"]
CODE_MODE_PHRASES = ["code mode"]
CODE_MODE_OFFER = "Sure thing twin, should I go into code mode?"
# Matched against already-normalized (lowercase, punctuation-stripped) text,
# so e.g. "how'd trading go" -> "howd trading go".
TRADING_SUMMARY_PHRASES = [
    "how did trading go", "howd trading go", "how is trading going", "hows trading going",
    "how did the trading go", "trading summary", "trading update", "how did trading do",
    "hows trading",
]
CHAT_MODEL = "claude-sonnet-4-5"
MAX_HISTORY_EXCHANGES = 10  # keep the last N user+Klaus exchanges, drop older ones
conversation_history = []
# Directory this script lives in, used as the working directory for subprocess calls
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
# Mirrors orb.py's own RESTART_FILE mechanism: an external editor (voice code
# mode, or a direct edit in this session) can't call restart_self() inside an
# already-running claus.py process directly, so it signals via this file
# instead, polled once per main-loop iteration.
CLAUS_RESTART_FILE = os.path.join(PROJECT_DIR, "claus_restart.txt")

def get_location():
    try:
        data = requests.get("http://ip-api.com/json/", timeout=5).json()
        if data.get("status") == "success":
            return f"{data['city']}, {data['regionName']}, {data['country']}"
    except requests.RequestException:
        pass
    return None

LOCATION = get_location()
print(f"Location: {LOCATION or 'unavailable'}")

SYSTEM_PROMPT = (
    "You are Klaus, a voice assistant with the personality of a regular guy "
    "shooting the shit at a bar. Blunt, deadpan, sarcastic — you don't "
    "sugarcoat anything and you're not impressed easily. Crude and a little "
    "rude is fine, keep it funny not mean, no slurs or actually hateful "
    "stuff. Drop a joke or a smartass remark when it fits, but still "
    "actually answer the question — don't let the bit get in the way of "
    "being useful. Keep answers short and conversational since they get "
    "read aloud. Use web search when you need current info you're not sure "
    "about. If fulfilling the request would require actually writing or "
    "changing code (fixing a bug, adding a feature, redesigning the orb, "
    "editing a file, etc.), don't try to do it yourself, don't claim you "
    "can't code, and don't hedge first with any disclaimer about that not "
    "being your lane, not being technical, or needing a coder — skip the "
    "preamble entirely and go straight to responding with EXACTLY this and "
    f"nothing else: \"{CODE_MODE_OFFER}\""
)
if LOCATION:
    SYSTEM_PROMPT += (
        f" The user's current approximate location (from IP geolocation) is "
        f"{LOCATION}. Use this for location-based questions (weather, time, "
        f"nearby places, etc.) unless the user specifies a different place."
    )

_pa = pyaudio.PyAudio()

@contextlib.contextmanager
def mic_level_meter():
    """Publishes live mic RMS level to the orb while active, best-effort."""
    stop_event = threading.Event()

    def run():
        rate = int(mic.SAMPLE_RATE) if mic.SAMPLE_RATE else 16000
        try:
            stream = _pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=rate,
                input=True,
                input_device_index=mic.device_index,
                frames_per_buffer=512,
            )
        except Exception as e:
            print(f"Mic meter unavailable: {e}")
            return
        try:
            while not stop_event.is_set():
                try:
                    data = stream.read(512, exception_on_overflow=False)
                except Exception:
                    break
                samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                rms = float(np.sqrt(np.mean(np.square(samples))))
                set_level(min(1.0, rms * 6))
        finally:
            stream.stop_stream()
            stream.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=1)
        set_level(0.0)

def speak(text):
    audio_chunks = elevenlabs_client.text_to_speech.convert(
        voice_id=ELEVENLABS_VOICE_ID,
        model_id="eleven_multilingual_v2",
        text=text,
    )
    audio_bytes = b"".join(audio_chunks)
    data, samplerate = sf.read(io.BytesIO(audio_bytes))
    mono = data if data.ndim == 1 else data.mean(axis=1)

    set_status("speaking")
    sd.play(data, samplerate)

    chunk = max(1, samplerate // 20)
    start_time = time.time()
    while sd.get_stream().active:
        idx = int((time.time() - start_time) * samplerate)
        window = mono[idx:idx + chunk]
        if len(window):
            rms = float(np.sqrt(np.mean(np.square(window))))
            set_level(min(1.0, rms * 4))
        time.sleep(0.05)

    sd.wait()
    set_level(0.0)
    set_status("idle")

def run_claude_code(instruction):
    """Returns (success, message). success is True only when Claude Code ran
    to completion without error, so the caller knows it's safe to restart."""
    prompt = (
        f"{instruction}. "
        "After making the changes, respond with ONLY a short 1-2 sentence "
        "plain-English summary of what you changed, suitable to be read "
        "aloud. No code, no markdown, no file paths unless essential."
    )
    try:
        result = subprocess.run(
            [
                "cmd", "/c", "claude",
                "-p", prompt,
                "--output-format", "json",
                "--model", "sonnet",
                "--permission-mode", "acceptEdits",
                "--allowedTools",
                "Read,Edit,Write,Glob,Grep,"
                "Bash(git *),Bash(pip install *),Bash(pytest*)",
                "--disallowedTools",
                "Bash(rm *),Bash(rmdir *),Bash(rd *),"
                "Bash(del *),Bash(erase *),Bash(format *)",
                "--permission-prompts", "none",
            ],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError:
        return False, "I can't find the Claude Code CLI. Is it installed?"
    except subprocess.TimeoutExpired:
        return False, "That took too long, I gave up."

    if result.returncode != 0:
        print(f"Claude Code error: {result.stderr}")
        return False, "Claude Code hit an error making those changes."

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"Claude Code raw output: {result.stdout}")
        return False, "Done, but I couldn't get a summary."

    if data.get("permission_denials"):
        print(f"Claude Code permission denials: {data['permission_denials']}")

    if data.get("is_error"):
        print(f"Claude Code error result: {data}")
        return False, "Claude Code hit an error making those changes."

    return True, data.get("result", "Done, but I couldn't get a summary.").strip()

def listen_for_instruction():
    instruction = None
    speech_error = False
    for attempt in range(2):
        print(f"Code mode: listening for instruction (attempt {attempt + 1})...")
        start = time.time()
        set_status("listening")
        with mic_level_meter(), mic as source:
            recognizer.adjust_for_ambient_noise(source, duration=0.5)
            audio2 = recognizer.listen(source, phrase_time_limit=15)
        print(f"Code mode: captured {time.time() - start:.1f}s of audio, recognizing...")

        try:
            instruction = recognizer.recognize_google(audio2)
            break
        except sr.UnknownValueError:
            if attempt == 0:
                print("Didn't catch that, say it again.")
                speak("Didn't catch that, say it again.")
        except sr.RequestError:
            print("Speech service hiccup, going back to standby.")
            speech_error = True
            break
    return instruction, speech_error

def listen_yes_no():
    set_status("listening")
    with mic_level_meter(), mic as source:
        recognizer.adjust_for_ambient_noise(source, duration=0.5)
        audio = recognizer.listen(source, phrase_time_limit=5)
    try:
        text = re.sub(r"[^\w\s]", "", recognizer.recognize_google(audio).lower())
    except (sr.UnknownValueError, sr.RequestError):
        return None
    print(f"Confirmation reply: {text}")
    if any(word in text for word in ("yes", "yeah", "yep", "yup", "correct", "right")):
        return True
    if any(word in text for word in ("no", "nope", "nah", "wrong")):
        return False
    return None

def restart_self():
    """Re-execs the current process in place so code-mode changes take
    effect immediately, without requiring a manual close/reopen."""
    print("Restarting to load code changes...")
    try:
        _pa.terminate()
    except Exception:
        pass
    os.execv(sys.executable, [sys.executable] + sys.argv)

def handle_code_mode(instruction):
    for _ in range(3):
        print(f"Confirming instruction: {instruction}")
        speak(f"You said: {instruction}. Did I get that right?")
        confirmed = listen_yes_no()

        if confirmed is True:
            break
        elif confirmed is False:
            speak("Okay, say it again.")
            instruction, speech_error = listen_for_instruction()
            if not instruction:
                if not speech_error:
                    speak("Still didn't catch that, never mind.")
                return
        else:
            speak("I didn't catch a yes or no. Was that right?")
    else:
        speak("Let's try this another time.")
        return

    print(f"Code instruction: {instruction}")
    speak("On it.")

    orb_path = os.path.join(PROJECT_DIR, "orb.py")
    orb_mtime_before = os.path.getmtime(orb_path) if os.path.exists(orb_path) else None

    success, summary = run_claude_code(instruction)
    print(f"Claude Code summary: {summary}")
    speak(summary)

    if success:
        orb_mtime_after = os.path.getmtime(orb_path) if os.path.exists(orb_path) else None
        if orb_mtime_after != orb_mtime_before:
            print("orb.py changed, requesting orb restart too...")
            request_restart()
        restart_self()

# Longest phrases first so e.g. "hey klaus" matches whole rather than
# leaving a stray "hey" behind after stripping just "klaus".
_WAKE_WORD_PATTERN = re.compile(
    r"^.*?\b(?:" + "|".join(re.escape(w) for w in sorted(WAKE_WORDS, key=len, reverse=True)) + r")\b\s*"
)

def strip_wake_word(heard):
    """Removes a leading wake word (and anything before it) from already-
    normalized text, returning whatever request/question is left, if any."""
    return _WAKE_WORD_PATTERN.sub("", heard, count=1).strip()

def handle_question(question):
    print(f"You said: {question}")
    normalized_question = re.sub(r"[^\w\s]", "", question.lower())

    if any(phrase in normalized_question for phrase in TRADING_SUMMARY_PHRASES):
        print("Trading summary requested.")
        try:
            from trading.daily_summary import get_todays_summary
            summary = get_todays_summary()
        except Exception as e:
            print(f"Trading summary error: {e}")
            summary = "Couldn't pull the trading summary right now, something broke on my end."
        print(f"Klaus says (trading summary): {summary}")
        speak(summary)
        return

    if any(phrase in normalized_question for phrase in CODE_MODE_PHRASES):
        instruction = re.sub(
            r"^.*?\bcode mode\b\s*", "", normalized_question, count=1
        ).strip()

        if not instruction:
            print("Code mode. What do you want done?")
            speak("Code mode. What do you want done?")
            time.sleep(0.3)
            instruction, speech_error = listen_for_instruction()
            if not instruction and not speech_error:
                print("Still didn't catch that, going back to standby.")
                speak("Still didn't catch that, never mind.")

        if instruction:
            handle_code_mode(instruction)
        return

    try:
        response = client.messages.create(
            model=CHAT_MODEL,
            max_tokens=150,
            system=SYSTEM_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=conversation_history + [{"role": "user", "content": question}]
        )
        answer = "".join(
            block.text for block in response.content if block.type == "text"
        )
        print(f"Klaus says: {answer}")
        speak(answer)

        conversation_history.append({"role": "user", "content": question})
        conversation_history.append({"role": "assistant", "content": answer})
        excess = len(conversation_history) - MAX_HISTORY_EXCHANGES * 2
        if excess > 0:
            del conversation_history[:excess]

        normalized_answer = re.sub(r"[^\w\s]", "", answer.lower())
        if "code mode" in normalized_answer:
            confirmed = listen_yes_no()
            if confirmed:
                handle_code_mode(question)
            elif confirmed is False:
                speak("Alright, never mind.")
            else:
                speak("Didn't catch a yes or no, never mind.")
    except anthropic.APIConnectionError:
        print("Lost connection to Claude, going back to standby.")
        speak("I'm having trouble connecting right now.")
    except anthropic.APIStatusError as e:
        print(f"Claude API error: {e}")
        speak("Something went wrong on my end.")

# Clear any stale restart request left over from a previous run so we don't
# immediately self-restart on startup.
if os.path.exists(CLAUS_RESTART_FILE):
    try:
        os.remove(CLAUS_RESTART_FILE)
    except OSError:
        pass

print("Klaus is standing by. Say a wake word to start...")
set_status("idle")

while True:
    if os.path.exists(CLAUS_RESTART_FILE):
        try:
            os.remove(CLAUS_RESTART_FILE)
        except OSError:
            pass
        restart_self()

    set_status("listening")
    with mic_level_meter(), mic as source:
        recognizer.adjust_for_ambient_noise(source, duration=0.5)
        try:
            # timeout bounds the wait for speech to START (phrase_time_limit
            # only bounds a phrase already in progress) so the loop reliably
            # comes back around to the restart check above even in silence,
            # instead of blocking here indefinitely.
            audio = recognizer.listen(source, timeout=5, phrase_time_limit=7)
        except sr.WaitTimeoutError:
            continue

    try:
        heard = recognizer.recognize_google(audio).lower()
        heard = re.sub(r"[^\w\s]", "", heard)
        print(f"Recognized: {heard}")
    except sr.UnknownValueError:
        continue
    except sr.RequestError:
        print("Speech service hiccup, trying again...")
        continue

    if any(phrase in heard for phrase in CODE_MODE_PHRASES):
        print("Code mode. What do you want done?")
        speak("Code mode. What do you want done?")
        time.sleep(0.3)

        instruction, speech_error = listen_for_instruction()
        if instruction:
            handle_code_mode(instruction)
        elif not speech_error:
            print("Still didn't catch that, going back to standby.")
            speak("Still didn't catch that, never mind.")

    elif any(word in heard for word in WAKE_WORDS):
        immediate_request = strip_wake_word(heard)
        print(f"Debug: strip_wake_word({heard!r}) -> {immediate_request!r}")

        if immediate_request:
            # Wake word and request came in the same breath (e.g. "klaus,
            # what's the weather") — skip the "I'm listening" round-trip
            # and act on it right away.
            handle_question(immediate_request)
        else:
            print("Yeah? I'm listening...")

            set_status("listening")
            with mic_level_meter(), mic as source:
                recognizer.adjust_for_ambient_noise(source, duration=0.5)
                audio2 = recognizer.listen(source, phrase_time_limit=12)

            try:
                question = recognizer.recognize_google(audio2)
                handle_question(question)
            except sr.UnknownValueError:
                print("Didn't catch that, going back to standby.")
            except sr.RequestError:
                print("Speech service hiccup, going back to standby.")