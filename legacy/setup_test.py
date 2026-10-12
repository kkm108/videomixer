import subprocess
import os
from pathlib import Path

def create_test_assets():
    """Create dummy video and audio files for testing."""
    os.makedirs("inputs", exist_ok=True)
    os.makedirs("library", exist_ok=True)
    
    # 1. Create a 10-second silent video
    print("Creating 10s test video...")
    subprocess.run([
        'ffmpeg', '-y', '-f', 'lavfi', '-i', 'testsrc=duration=10:size=640x360:rate=30',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=10', # Add some tone as "original audio"
        '-c:v', 'libx264', '-c:a', 'aac', 'inputs/test_video_10s.mp4'
    ], capture_output=True)

    # 2. Create a 30-second silent video
    print("Creating 30s test video...")
    subprocess.run([
        'ffmpeg', '-y', '-f', 'lavfi', '-i', 'testsrc=duration=30:size=640x360:rate=30',
        '-c:v', 'libx264', '-an', 'inputs/silent_video_30s.mp4' # Silent video
    ], capture_output=True)

    # 3. Create library audio files
    audio_durations = [10, 15, 30, 45, 60]
    for dur in audio_durations:
        print(f"Creating {dur}s library audio...")
        # Use different frequencies to distinguish them
        freq = 200 + (dur * 10)
        subprocess.run([
            'ffmpeg', '-y', '-f', 'lavfi', '-i', f'sine=frequency={freq}:duration={dur}',
            '-c:a', 'mp3', f'library/bgm_{dur}s.mp3'
        ], capture_output=True)

    # 4. Create SFX files
    os.makedirs("sfx", exist_ok=True)
    for name in ["impact_1", "impact_2"]:
        print(f"Creating SFX {name}...")
        subprocess.run([
            'ffmpeg', '-y', '-f', 'lavfi', '-i', f'sine=frequency=800:duration=0.5',
            '-c:a', 'mp3', f'sfx/{name}.mp3'
        ], capture_output=True)

if __name__ == "__main__":
    create_test_assets()
    print("Test assets created successfully.")
