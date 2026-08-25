import sys
import time

def test(name, fn):
    t0 = time.time()
    print(f"Testing {name}...", end=" ", flush=True)
    try:
        fn()
        print(f"OK ({time.time() - t0:.2f}s)", flush=True)
    except Exception as e:
        print(f"FAILED ({e})", flush=True)

test("FastAPI", lambda: __import__("fastapi"))
test("Torch", lambda: __import__("torch"))
test("TorchVision", lambda: __import__("torchvision"))
test("TorchAudio", lambda: __import__("torchaudio"))
test("OpenCV", lambda: __import__("cv2"))
test("Ultralytics", lambda: __import__("ultralytics"))
test("Scikit-Learn", lambda: __import__("sklearn"))
print("Done all core tests!", flush=True)
