"""
SENTRIX dataset loaders.

Class indices are fixed and identical in both loaders below:

    0 normal   1 fire   2 weapon   3 assault   4 robbery

Usage:
    from load_dataset import CLASSES, CLASS_TO_IDX, imagefolder_split, manifest_split
"""
from __future__ import annotations

import csv
from pathlib import Path

try:                                     # torch is optional for inspection
    from torch.utils.data import Dataset
except ModuleNotFoundError:              # lets you run this file to print counts
    Dataset = object

ROOT = Path(__file__).resolve().parent

CLASSES = ["normal", "fire", "weapon", "assault", "robbery"]
CLASS_TO_IDX = {c: i for i, c in enumerate(CLASSES)}
IDX_TO_CLASS = {i: c for c, i in CLASS_TO_IDX.items()}
FOLDER_NAMES = [f"{i}_{c}" for i, c in enumerate(CLASSES)]      # 0_normal, 1_fire, ...


# --------------------------------------------------------------------------
# Option A: torchvision ImageFolder.
# The numeric folder prefixes make ImageFolder's alphabetical sort produce
# exactly CLASS_TO_IDX, so no override is needed. It returns class names as
# "0_normal" etc.; use strip_prefix() to get back to "normal".
# --------------------------------------------------------------------------
def imagefolder_split(split: str, transform=None):
    """split: 'train' | 'val' | 'test'."""
    from torchvision.datasets import ImageFolder
    try:
        # torchvision >= 0.19: needed because val/test have empty class folders
        ds = ImageFolder(ROOT / split, transform=transform, allow_empty=True)
    except TypeError:
        ds = ImageFolder(ROOT / split, transform=transform)
    assert [strip_prefix(c) for c in ds.classes] == CLASSES, (
        f"unexpected class order in {split}: {ds.classes}")
    return ds


def strip_prefix(folder_name: str) -> str:
    """'2_weapon' -> 'weapon'"""
    return folder_name.split("_", 1)[1]


# --------------------------------------------------------------------------
# Option B: read labels.csv. Immune to empty folders and to any future
# directory reshuffle, and gives you the provenance columns as well.
# --------------------------------------------------------------------------
class SentrixFrames(Dataset):
    def __init__(self, split: str, transform=None, root: Path = ROOT):
        self.root = Path(root)
        self.transform = transform
        with (self.root / "labels.csv").open(newline="", encoding="utf-8") as fh:
            self.rows = [r for r in csv.DictReader(fh) if r["split"] == split]
        if not self.rows:
            raise ValueError(f"no rows for split {split!r} in labels.csv")

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        from PIL import Image
        r = self.rows[i]
        img = Image.open(self.root / r["filepath"]).convert("RGB")
        if self.transform is not None:
            img = self.transform(img)
        return img, int(r["class_id"])

    def provenance(self, i) -> dict:
        """Source video, frame index, timestamp and how the label was assigned."""
        return dict(self.rows[i])


def manifest_split(split: str, transform=None) -> SentrixFrames:
    return SentrixFrames(split, transform)


if __name__ == "__main__":
    from collections import Counter
    print("class_to_idx:", CLASS_TO_IDX)
    for s in ("train", "val", "test"):
        ds = manifest_split(s)
        c = Counter(int(r["class_id"]) for r in ds.rows)
        print(f"{s:5} {len(ds):4}  " + "  ".join(
            f"{i}:{IDX_TO_CLASS[i]}={c.get(i, 0)}" for i in range(len(CLASSES))))
