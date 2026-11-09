import sys, time, traceback
from faster_whisper import WhisperModel
src, out = sys.argv[1], sys.argv[2]
def main():
    t = time.time()
    m = WhisperModel("medium", device="cpu", compute_type="int8", cpu_threads=4)
    segs, info = m.transcribe(src, language="ko", beam_size=1, vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
                              condition_on_previous_text=False)
    n = 0
    with open(out, "w", encoding="utf-8") as f:
        for s in segs:
            mm, ss = divmod(int(s.start), 60)
            f.write(f"[{mm:02d}:{ss:02d}] {s.text.strip()}\n")
            n += 1
            if n % 100 == 0:
                print(f"{n} segs, at {s.start/60:.1f} min, elapsed {(time.time()-t)/60:.1f} min", flush=True)
    print(f"done {n} segs, audio {info.duration/60:.1f} min, took {(time.time()-t)/60:.1f} min")
    print(f"::warning title=stt::{n} segs, audio {info.duration/60:.1f} min, took {(time.time()-t)/60:.1f} min")


try:
    main()
except Exception:
    tb = traceback.format_exc().replace("\n", " | ")
    print("::warning title=stt_err::" + tb[-1500:])
    raise
