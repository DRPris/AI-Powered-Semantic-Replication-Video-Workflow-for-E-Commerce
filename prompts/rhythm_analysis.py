"""
视频节奏分析 Prompt 模板
对视频进行画面与声音联合分析，提取节奏结构供复刻剪辑参考
"""

RHYTHM_ANALYSIS_PROMPT = """You are an expert video editor and rhythm analyst. Your task is to analyze the COMBINED audio-visual rhythm of this video to produce a replication rhythm guide.

The goal is NOT to describe what is happening — the goal is to extract the RHYTHMIC STRUCTURE so that an editor can replicate the same pacing and feel in a new video, even with different content.

## Analysis Requirements

Watch and listen simultaneously. For every observation, connect WHAT YOU SEE with WHAT YOU HEAR at the same moment.

### 1. Overall Rhythm Overview
- Total duration (seconds)
- Total number of shots/cuts
- Average shot duration
- Overall pace feel: fast / medium / slow
- Pace pattern across the whole video: steady / accelerating / decelerating / wave (starts slow, builds, peaks, slows)

### 2. Audio Track Analysis
- Music genre / style (electronic, acoustic, hip-hop, ambient, etc.)
- Estimated BPM (beats per minute) — observe the drum or bass beat pattern
- List the timestamps (in seconds) where strong beats or rhythmic accents land
- Divide the video into audio mood segments (e.g., 0–8s: calm intro, 8–20s: energy build, 20–35s: peak)

### 3. Shot-by-Shot Rhythm Data
For EACH shot/cut, document:
- Shot number, start time, end time, duration (all in seconds)
- Pace classification: slow (>3s) / medium (1.5–3s) / fast (<1.5s)
- Visual intensity score 0.0–1.0 (0=static wide shot, 1=extreme fast motion or dramatic cut)
- Audio intensity score 0.0–1.0 at this shot's start moment
- Cut type entering this shot: hard_cut / dissolve / wipe / fade / smash_cut
- Camera motion: static / slow_pan / fast_pan / push_in / pull_out / fast_zoom / handheld / other
- Beat-aligned: true if the cut lands on or within 0.1s of a strong beat; false otherwise
- Sync description: one sentence explaining the audio-visual sync at this cut point

### 4. Rhythm Timeline — Strong Rhythm Points
Identify the KEY MOMENTS where audio and visual rhythms converge for maximum impact. These are the moments that define the video's energy. For each:
- Timestamp in seconds
- Type: beat_sync / transition / climax / pause / text_reveal / music_drop
- Visual trigger: what visually happens (e.g., "hard cut to extreme close-up")
- Audio trigger: what audibly happens (e.g., "bass drop", "beat hit", "silence")
- Combined impact level: high / medium / low
- Replication note: specific instruction for recreating this moment in a new video

### 5. Replication Rhythm Guide
Synthesize everything into actionable instructions:
- Describe the overall rhythmic contract of this video (the "feel" the editor must replicate)
- List the MUST-SYNC moments: specific timestamps where cuts must land on beats
- Define pace zones: break the video into time segments with target cut counts and pace description
- State the audio-cut alignment rule: when should cuts generally happen relative to the music beat (e.g., "cuts land on every 2nd beat", "cuts anticipate the beat by 0.1s")
- List any rhythmic patterns to avoid (e.g., "never hold a shot longer than 4s after 0:15")

## Output Format

Output ONLY valid JSON. No explanation before or after. Use this exact structure:

{
  "overview": {
    "total_duration_sec": 0.0,
    "total_shots": 0,
    "avg_shot_duration_sec": 0.0,
    "overall_pace": "fast|medium|slow",
    "pace_pattern": "steady|accelerating|decelerating|wave"
  },
  "audio": {
    "music_type": "...",
    "estimated_bpm": 0,
    "beat_positions_sec": [],
    "audio_segments": [
      {
        "start_sec": 0,
        "end_sec": 0,
        "mood": "...",
        "energy": "low|medium|high",
        "description": "..."
      }
    ]
  },
  "shots": [
    {
      "shot_number": 1,
      "start_sec": 0.0,
      "end_sec": 0.0,
      "duration_sec": 0.0,
      "pace": "slow|medium|fast",
      "visual_intensity": 0.0,
      "audio_intensity": 0.0,
      "cut_type": "hard_cut|dissolve|wipe|fade|smash_cut",
      "motion": "...",
      "beat_aligned": true,
      "sync_description": "..."
    }
  ],
  "rhythm_timeline": [
    {
      "timestamp_sec": 0.0,
      "type": "beat_sync|transition|climax|pause|text_reveal|music_drop",
      "visual_trigger": "...",
      "audio_trigger": "...",
      "combined_impact": "high|medium|low",
      "replication_note": "..."
    }
  ],
  "replication_rhythm_guide": {
    "rhythmic_contract": "...",
    "must_sync_moments": [],
    "pace_zones": [
      {
        "zone": "0-Xs",
        "target_cuts": 0,
        "target_pace": "...",
        "description": "..."
      }
    ],
    "audio_cut_alignment_rule": "...",
    "patterns_to_avoid": []
  }
}
"""
