#!/usr/bin/env bash
# usage: run.sh NAME ROBBERY(0|1) SCENE(v4|v5)
S="/c/Users/PC/AppData/Local/Temp/claude/324a87da11b408e0/03a07f5d-2599-44c3-b7d9-bc380698ab1f/scratchpad/robbery"
NAME=$1; ROB=$2; SCENE=$3
OUT='G:\Sentrix\training\runs\custom_eval\robbery_l3_2026-11-21\'"$NAME"
mkdir -p "$S/var_$NAME"
export SENTRIX_VAR_DIR="$(cygpath -w "$S/var_$NAME")"
export SENTRIX_TEST_MODE=1 SENTRIX_LIVE_ALERTS=0 SENTRIX_SOS_ENABLED=0 SENTRIX_LINK_ENABLED=0
export SENTRIX_PIPELINE_MODE=clip SENTRIX_RUNTIME=torch SENTRIX_CLIP_IMGSZ=320 SENTRIX_FAST_LANE=1
export SENTRIX_CLIP_THREADS=4 SENTRIX_FACE_DET_SIZE=320 SENTRIX_FACE_MODEL=buffalo_s
export SENTRIX_SCENE_THRESHOLDS='{"weapon": 0.78, "fire": 0.75, "robbery": 0.60}'
export SENTRIX_ROBBERY_ALARM=$ROB
export HF_HUB_OFFLINE=1 YOLO_OFFLINE=1 TRANSFORMERS_OFFLINE=1
if [ "$SCENE" = v5 ]; then export SENTRIX_SCENE_WEIGHTS='G:\Sentrix\.claude\worktrees\robbery-l3\models\v5_room\scene_classifier_room_v5.pt'; fi
VIDS=vid02,vid03,vid05,vid06,vid07,wa01,wa03,mmtheft,mmassault,mmgun,mmknife,nvroom2,vid04,gun
/g/Sentrix/.venv/Scripts/python.exe "$(cygpath -w "$S/drive.py")" "$OUT" "$VIDS" > "$S/log_$NAME.txt" 2>&1
echo "exit $?" >> "$S/log_$NAME.txt"
