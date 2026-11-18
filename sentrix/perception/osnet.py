"""Body appearance embeddings: OSNet, the re-identification model family of
IntrusionSentry (github.com/praxshant/IntrusionSentry, the team's earlier
intrusion-detection prototype).

IntrusionSentry recognised enrolled people by their whole body, not only their
face: ``torchreid.utils.FeatureExtractor("osnet_x1_0")`` turned a person crop
into a 512-d vector, compared by cosine similarity (threshold 0.50), fused
0.6 body + 0.4 face when a face was visible. This module reproduces that
model without torchreid (it does not install on the app's Python and pulls
tensorboard, gdown and Cython):

* **Networks**: OSNet x1_0 (Zhou et al., "Omni-Scale Feature Learning for
  Person Re-Identification", ICCV 2019) and OSNet-AIN x1_0 (Zhou et al.,
  "Learning Generalisable Omni-Scale Representations for Person
  Re-Identification", TPAMI 2021: the same size, with instance normalisation
  for unseen domains), both with torchreid's module and parameter names
  (code MIT, KaiyangZhou/deep-person-reid), so its checkpoints load unchanged;
  the training classifier is dropped. In eval mode the output is the 512-d
  ``fc`` feature (Linear + BatchNorm + ReLU). :func:`architecture` tells the
  two apart from a checkpoint's keys.
* **Weights**: ``osnet_ain_x1_0_msmt17.pth`` (17.3 MB, 2.2 M parameters),
  OSNet-AIN trained for re-identification on MSMT17 (torchreid model zoo,
  "MSMT17 -> Market1501 & DukeMTMC-reID": the best cross-domain OSNet there).
  IntrusionSentry used OSNet x1_0 with **ImageNet** weights (what torchreid
  downloads when ``model_path`` is empty); on the nine train + val custom
  videos those separated people worse (pairwise AUROC 0.83 vs 0.93; the
  identity rule's same-person acceptance at <= 0.05 % strangers 37 % vs 42 %,
  ``training/runs/reid/osnet_weights_compare/REPORT.md``), so they are no
  longer shipped (git ``9eb50a1`` holds them). Loaded with torch's
  weights-only unpickler; ``tools/fetch_models.py`` downloads and checks them.
* **Pre-processing** (IntrusionSentry + torchreid): the person box clipped to
  the frame, padded sideways by replicating its edge when narrower than 0.4
  of its height (to 0.5), RGB, resized to 256 x 128 with PIL's bilinear filter
  (what ``torchvision.transforms.Resize`` does to a PIL image), ImageNet
  mean/std, then the feature L2-normalised.

It runs on its own onnxruntime session (the ``.onnx`` file next to the
weights, exported by ``tools/fetch_models.py --export``, intra-op
``settings.reid.threads``, spinning off) so it never touches torch's
process-wide threads; without onnxruntime or the ONNX file it falls back to
torch. The similarity bars in ``settings.reid`` belong to these weights:
other weights need ``tools/reid_eval.py`` and new bars.

Where SENTRIX uses it: ``sentrix/perception/reid.py`` (global ids, and a
face-verified resident's look carried to their own new track, CLAUDE.md D24).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from sentrix.utils.logging import get_logger

log = get_logger("osnet")

WEIGHTS_NAME = "osnet_ain_x1_0_msmt17.pth"
WEIGHTS_SHA256 = "8a07e8da38946f7cee37f4561617bf8b6d2fe8f3a4027852893ea092e46d919f"
#: torchreid's model-zoo link (docs/MODEL_ZOO.md, osnet_ain_x1_0, MSMT17 ->
#: Market1501 & DukeMTMC-reID): osnet_ain_x1_0_msmt17_256x128_amsgrad_ep50_
#: lr0.0015_coslr_b64_fb10_softmax_labsmth_flip_jitter.pth
WEIGHTS_URL = ("https://drive.usercontent.google.com/download"
               "?id=1SigwBE6mPdqiJMqhuIY4aqC7--5CsMal&export=download")
ONNX_NAME = "osnet_ain_x1_0_msmt17.onnx"

HEIGHT, WIDTH = 256, 128
FEATURE_DIM = 512
_MEAN = np.array([0.485, 0.456, 0.406], np.float32).reshape(1, 3, 1, 1)
_STD = np.array([0.229, 0.224, 0.225], np.float32).reshape(1, 3, 1, 1)
#: IntrusionSentry pads a crop whose width/height is under 0.8 * 0.5.
_TARGET_ASPECT = 0.5


def body_crop(frame: np.ndarray, box) -> np.ndarray | None:
    """The person's crop as IntrusionSentry took it (BGR), or None if empty."""
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = (int(v) for v in box)
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(width, x2), min(height, y2)
    if x2 - x1 < 2 or y2 - y1 < 2:
        return None
    crop = frame[y1:y2, x1:x2]
    if crop.shape[1] / crop.shape[0] < _TARGET_ASPECT * 0.8:
        pad = max(0, (int(crop.shape[0] * _TARGET_ASPECT) - crop.shape[1]) // 2)
        crop = np.pad(crop, ((0, 0), (pad, pad), (0, 0)), mode="edge")
    return crop


def preprocess(crops: list[np.ndarray]) -> np.ndarray:
    """BGR crops -> (N, 3, 256, 128) float32, normalised like torchreid."""
    from PIL import Image
    batch = np.empty((len(crops), 3, HEIGHT, WIDTH), np.float32)
    for i, crop in enumerate(crops):
        rgb = np.ascontiguousarray(crop[:, :, ::-1])
        image = Image.fromarray(rgb).resize((WIDTH, HEIGHT), Image.BILINEAR)
        batch[i] = np.asarray(image, np.float32).transpose(2, 0, 1) / 255.0
    return (batch - _MEAN) / _STD


#: The two networks this module can build (torchreid's names).
ARCHITECTURES = ("osnet_x1_0", "osnet_ain_x1_0")


def _build_osnet(arch: str = "osnet_x1_0"):
    """OSNet x1_0, or OSNet-AIN x1_0 (Zhou et al., TPAMI 2021: instance
    normalisation in the first conv and inside chosen blocks, for domain
    generalisation), with torchreid's module and parameter names; the forward
    returns the eval feature (fc output)."""
    import torch.nn as nn
    import torch.nn.functional as F

    if arch not in ARCHITECTURES:
        raise ValueError(f"unknown OSNet architecture {arch!r}; use one of {ARCHITECTURES}")
    ain = arch == "osnet_ain_x1_0"

    class ConvLayer(nn.Module):
        def __init__(self, cin, cout, k, stride=1, padding=0, instance_norm=False):
            super().__init__()
            self.conv = nn.Conv2d(cin, cout, k, stride=stride, padding=padding, bias=False)
            self.bn = (nn.InstanceNorm2d(cout, affine=True) if instance_norm
                       else nn.BatchNorm2d(cout))
            self.relu = nn.ReLU(inplace=True)

        def forward(self, x):
            return self.relu(self.bn(self.conv(x)))

    class Conv1x1(nn.Module):
        def __init__(self, cin, cout):
            super().__init__()
            self.conv = nn.Conv2d(cin, cout, 1, bias=False)
            self.bn = nn.BatchNorm2d(cout)
            self.relu = nn.ReLU(inplace=True)

        def forward(self, x):
            return self.relu(self.bn(self.conv(x)))

    class Conv1x1Linear(nn.Module):
        def __init__(self, cin, cout, bn=True):
            super().__init__()
            self.conv = nn.Conv2d(cin, cout, 1, bias=False)
            self.bn = nn.BatchNorm2d(cout) if bn else None

        def forward(self, x):
            x = self.conv(x)
            return x if self.bn is None else self.bn(x)

    class LightConv3x3(nn.Module):
        def __init__(self, cin, cout):
            super().__init__()
            self.conv1 = nn.Conv2d(cin, cout, 1, bias=False)
            self.conv2 = nn.Conv2d(cout, cout, 3, padding=1, bias=False, groups=cout)
            self.bn = nn.BatchNorm2d(cout)
            self.relu = nn.ReLU(inplace=True)

        def forward(self, x):
            return self.relu(self.bn(self.conv2(self.conv1(x))))

    class ChannelGate(nn.Module):
        def __init__(self, channels, reduction=16):
            super().__init__()
            self.global_avgpool = nn.AdaptiveAvgPool2d(1)
            self.fc1 = nn.Conv2d(channels, channels // reduction, 1, bias=True)
            self.relu = nn.ReLU(inplace=True)
            self.fc2 = nn.Conv2d(channels // reduction, channels, 1, bias=True)
            self.gate_activation = nn.Sigmoid()

        def forward(self, x):
            gate = self.fc2(self.relu(self.fc1(self.global_avgpool(x))))
            return x * self.gate_activation(gate)

    class OSBlock(nn.Module):
        """OSNet's block: four streams of 1-4 light 3x3 convs (conv2a-d)."""

        def __init__(self, cin, cout, bottleneck_reduction=4):
            super().__init__()
            mid = cout // bottleneck_reduction
            self.conv1 = Conv1x1(cin, mid)
            self.conv2a = LightConv3x3(mid, mid)
            self.conv2b = nn.Sequential(*(LightConv3x3(mid, mid) for _ in range(2)))
            self.conv2c = nn.Sequential(*(LightConv3x3(mid, mid) for _ in range(3)))
            self.conv2d = nn.Sequential(*(LightConv3x3(mid, mid) for _ in range(4)))
            self.gate = ChannelGate(mid)
            self.conv3 = Conv1x1Linear(mid, cout)
            self.downsample = Conv1x1Linear(cin, cout) if cin != cout else None

        def forward(self, x):
            x1 = self.conv1(x)
            x2 = (self.gate(self.conv2a(x1)) + self.gate(self.conv2b(x1))
                  + self.gate(self.conv2c(x1)) + self.gate(self.conv2d(x1)))
            identity = x if self.downsample is None else self.downsample(x)
            return F.relu(self.conv3(x2) + identity)

    class LightConvStream(nn.Module):
        def __init__(self, channels, depth):
            super().__init__()
            self.layers = nn.Sequential(*(LightConv3x3(channels, channels)
                                          for _ in range(depth)))

        def forward(self, x):
            return self.layers(x)

    class AinBlock(nn.Module):
        """OSNet-AIN's block (torchreid ``OSBlock`` / ``OSBlockINin``): the same
        four streams as a module list; with ``instance_norm`` the residual
        branch ends in instance norm instead of batch norm."""

        def __init__(self, cin, cout, instance_norm, reduction=4):
            super().__init__()
            mid = cout // reduction
            self.conv1 = Conv1x1(cin, mid)
            self.conv2 = nn.ModuleList(LightConvStream(mid, t) for t in range(1, 5))
            self.gate = ChannelGate(mid)
            self.conv3 = Conv1x1Linear(mid, cout, bn=not instance_norm)
            self.downsample = Conv1x1Linear(cin, cout) if cin != cout else None
            self.IN = nn.InstanceNorm2d(cout, affine=True) if instance_norm else None

        def forward(self, x):
            x1 = self.conv1(x)
            x2 = 0
            for stream in self.conv2:
                x2 = x2 + self.gate(stream(x1))
            x3 = self.conv3(x2)
            if self.IN is not None:
                x3 = self.IN(x3)
            identity = x if self.downsample is None else self.downsample(x)
            return F.relu(x3 + identity)

    def pool(channels):
        return nn.Sequential(Conv1x1(channels, channels), nn.AvgPool2d(2, stride=2))

    class OSNet(nn.Module):
        def __init__(self, channels=(64, 256, 384, 512)):
            super().__init__()
            self.conv1 = ConvLayer(3, channels[0], 7, stride=2, padding=3, instance_norm=ain)
            self.maxpool = nn.MaxPool2d(3, stride=2, padding=1)
            if ain:
                # torchreid osnet_ain_x1_0: [INin, INin], [plain, INin], [INin, plain]
                pattern = ((True, True), (False, True), (True, False))
                self.conv2 = nn.Sequential(*(AinBlock(cin, channels[1], n) for cin, n in
                                             zip((channels[0], channels[1]), pattern[0])))
                self.pool2 = pool(channels[1])
                self.conv3 = nn.Sequential(*(AinBlock(cin, channels[2], n) for cin, n in
                                             zip((channels[1], channels[2]), pattern[1])))
                self.pool3 = pool(channels[2])
                self.conv4 = nn.Sequential(*(AinBlock(cin, channels[3], n) for cin, n in
                                             zip((channels[2], channels[3]), pattern[2])))
            else:
                self.conv2 = nn.Sequential(OSBlock(channels[0], channels[1]),
                                           OSBlock(channels[1], channels[1]), pool(channels[1]))
                self.conv3 = nn.Sequential(OSBlock(channels[1], channels[2]),
                                           OSBlock(channels[2], channels[2]), pool(channels[2]))
                self.conv4 = nn.Sequential(OSBlock(channels[2], channels[3]),
                                           OSBlock(channels[3], channels[3]))
            self.conv5 = Conv1x1(channels[3], channels[3])
            self.global_avgpool = nn.AdaptiveAvgPool2d(1)
            self.fc = nn.Sequential(nn.Linear(channels[3], FEATURE_DIM),
                                    nn.BatchNorm1d(FEATURE_DIM), nn.ReLU(inplace=True))

        def forward(self, x):                       # (N, 3, 256, 128) -> (N, 512)
            x = self.maxpool(self.conv1(x))
            if ain:
                x = self.conv4(self.pool3(self.conv3(self.pool2(self.conv2(x)))))
            else:
                x = self.conv4(self.conv3(self.conv2(x)))
            x = self.conv5(x)
            return self.fc(self.global_avgpool(x).flatten(1))

    return OSNet()


def _state_dict(weights: Path) -> dict:
    """The checkpoint's tensors (weights-only unpickler), without a
    ``module.`` prefix and without the training classifier.

    Denormal (subnormal) values are set to zero: the MSMT17-trained
    checkpoints hold 20 k - 176 k of them, which made CPU inference 3-17x
    slower (OSNet-AIN 72 -> 11 ms a crop with this and the session's
    flush-to-zero); a weight under 1.2e-38 cannot change a feature."""
    import torch
    state = torch.load(str(weights), map_location="cpu", weights_only=True)
    state = state.get("state_dict", state)
    out = {}
    for key, value in state.items():
        key = key.removeprefix("module.")
        if key.startswith("classifier."):
            continue
        if value.is_floating_point():
            tiny = torch.finfo(value.dtype).tiny
            value = value.masked_fill((value.abs() < tiny) & (value != 0), 0.0)
        out[key] = value
    return out


def architecture(state: dict) -> str:
    """Which OSNet a checkpoint holds: OSNet-AIN has instance norm in blocks."""
    return "osnet_ain_x1_0" if "conv2.0.IN.weight" in state else "osnet_x1_0"


def load_torch_osnet(weights: Path):
    """The torch network with the checkpoint loaded strictly: every parameter
    except the training classifier must match by name and shape."""
    state = _state_dict(weights)
    net = _build_osnet(architecture(state))
    net.load_state_dict(state, strict=True)
    return net.eval()


def export_onnx(weights: Path, target: Path) -> float:
    """Export to ONNX (dynamic batch); returns the largest absolute difference
    to torch on a random batch (parity check)."""
    import torch
    net = load_torch_osnet(weights)
    target.parent.mkdir(parents=True, exist_ok=True)
    torch.onnx.export(net, (torch.zeros(1, 3, HEIGHT, WIDTH),), str(target),
                      input_names=["image"], output_names=["feature"], opset_version=17,
                      dynamo=False, dynamic_axes={"image": {0: "batch"},
                                                  "feature": {0: "batch"}})
    import onnxruntime as ort
    session = ort.InferenceSession(str(target), providers=["CPUExecutionProvider"])
    batch = np.random.default_rng(0).normal(0, 1, (3, 3, HEIGHT, WIDTH)).astype(np.float32)
    got = session.run(None, {"image": batch})[0]
    with torch.no_grad():
        want = net(torch.from_numpy(batch)).numpy()
    return float(np.abs(got - want).max())


class BodyEmbedder:
    """An OSNet checkpoint on its own onnxruntime session (the ``.onnx`` file
    next to it; torch fallback). ``available`` is False when the weights are
    missing; nothing is downloaded here."""

    def __init__(self, weights: Path, *, threads: int | None = None,
                 onnx: Path | None = None) -> None:
        self.available = False
        self.backend = "none"
        self.name = "none"
        self.weights = weights.name
        self._session = None
        self._model = None
        self._torch = None
        if not weights.exists():
            log.info("re-id weights not found (%s); run tools/fetch_models.py", weights)
            return
        try:
            self.name = architecture(_state_dict(weights))
            onnx = onnx if onnx is not None else weights.with_suffix(".onnx")
            if onnx.exists() and self._load_onnx(onnx, threads):
                self.backend = "onnx"
            else:
                import torch
                self._model, self._torch = load_torch_osnet(weights), torch
                self.backend = "torch"
            probe = self.embed([np.zeros((64, 32, 3), np.uint8)])
            if probe.shape != (1, FEATURE_DIM):
                log.error("re-id model: output %s, expected (1, %d)", probe.shape, FEATURE_DIM)
                return
            self.available = True
            log.info("re-id model loaded: %s from %s (%s)", self.name, self.weights,
                     self.backend)
        except Exception as exc:
            log.error("re-id model failed to load: %s", exc)

    def _load_onnx(self, path: Path, threads: int | None) -> bool:
        try:
            import onnxruntime as ort
        except ImportError:
            return False
        try:
            options = ort.SessionOptions()
            options.intra_op_num_threads = max(1, int(threads or 2))
            options.inter_op_num_threads = 1
            options.add_session_config_entry("session.intra_op.allow_spinning", "0")
            # Flush denormal activations to zero on this session's threads only.
            options.add_session_config_entry("session.set_denormal_as_zero", "1")
            self._session = ort.InferenceSession(str(path), options,
                                                 providers=["CPUExecutionProvider"])
            return True
        except Exception as exc:
            log.warning("re-id ONNX %s unusable (%s); using torch", path.name, exc)
            self._session = None
            return False

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        """Unit-length 512-d features, one row per BGR crop."""
        if not crops:
            return np.zeros((0, FEATURE_DIM), np.float32)
        batch = preprocess(crops)
        if self._session is not None:
            features = self._session.run(None, {"image": batch})[0]
        else:
            torch = self._torch
            with torch.no_grad():
                features = self._model(torch.from_numpy(batch)).numpy()
        norms = np.linalg.norm(features, axis=1, keepdims=True)
        return (features / np.maximum(norms, 1e-8)).astype(np.float32)


__all__ = ["ARCHITECTURES", "BodyEmbedder", "FEATURE_DIM", "HEIGHT", "ONNX_NAME",
           "WEIGHTS_NAME", "WEIGHTS_SHA256", "WEIGHTS_URL", "WIDTH", "architecture",
           "body_crop", "export_onnx", "load_torch_osnet", "preprocess"]
