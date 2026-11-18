# Label audit

Data: `G:\Capstone\data` and `training\datasets\sentrix_custom`.

## weapon_data (knife, long_gun, pistol)

| split | images | label files | no label file | empty | boxes per class | problems |
|---|---|---|---|---|---|---|
| train | 7170 | 7170 | 0 | 409 | {'knife': 2857, 'long_gun': 947, 'pistol': 3856} | none |
| valid | 1599 | 1599 | 0 | 102 | {'knife': 730, 'long_gun': 258, 'pistol': 678} | none |
| test | 703 | 703 | 0 | 47 | {'knife': 316, 'long_gun': 148, 'pistol': 254} | none |

Byte-identical images: {'across_splits': {'train~valid': 0, 'train~test': 0, 'valid~test': 0}, 'within_split_extra_copies': {'train': 0, 'valid': 0, 'test': 0}}

## fire_smoke_data (fire, smoke)

| split | images | label files | no label file | empty | boxes per class | problems |
|---|---|---|---|---|---|---|
| train | 10000 | 10000 | 0 | 4549 | {'fire': 5562, 'smoke': 6901} | {'zero_size_box': 10} |
| valid | 2000 | 2000 | 0 | 931 | {'fire': 1083, 'smoke': 1392} | {'zero_size_box': 3, 'coord_outside_0_1': 7} |

Byte-identical images: {'across_splits': {'train~valid': 0}, 'within_split_extra_copies': {'train': 0, 'valid': 0}}

## violence_data

| class | images | source groups | empty | undecodable (sample) |
|---|---|---|---|---|
| fight | 10110 | 1011 | 0 | 0 / 300 |
| nofight | 9500 | 950 | 0 | 0 / 300 |

## anomaly_data

| class | images | source groups | empty | undecodable (sample) |
|---|---|---|---|---|
| anomalous | 1607 | 200 | 0 | 0 / 300 |
| normal | 1600 | 200 | 0 | 0 / 300 |

## audio_data (AudioSet clips)

| class | files | formats | WAV seconds | unreadable |
|---|---|---|---|---|
| fire | 106 | {'.wav': 105, '.webm': 1} | 850.4 | 0 |
| scream | 60 | {'.wav': 54, '.webm': 5, '.m4a': 1} | 392.9 | 1 |
| siren | 174 | {'.wav': 154, '.webm': 15, '.part': 3, '.m4a': 2} | 1259.3 | 0 |
| speech_normal | 177 | {'.wav': 156, '.part': 4, '.m4a': 4, '.webm': 13} | 1179.6 | 1 |

## SENTRIX custom dataset

977 labelled images from 15 videos (1707 sampled frames, 84 segments).

Per class and split: {'normal': {'train': 463, 'val': 23, 'test': 67}, 'fire': {'train': 11, 'val': 6}, 'weapon': {'train': 191, 'val': 55, 'test': 26}, 'assault': {'train': 56}, 'robbery': {'train': 79}}

Frames by status: {'near_duplicate': 656, 'ok': 764, 'resolved_by_frame_level_visual_review': 213, 'blurry': 7, 'ambiguous_low_confidence': 67}

Boxes: {'fire': 17, 'pistol': 119, 'stick': 38, 'knife': 6}; by label and status {'assault:excluded': 56, 'fire:box': 17, 'normal:negative': 553, 'robbery:excluded': 79, 'weapon:box': 163, 'weapon:no_confident_box': 79, 'weapon:rejected_in_qc': 30}

Problems: none
