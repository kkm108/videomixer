import os
import subprocess
import json
import random
from pathlib import Path

class VideoMixer:
    def __init__(self, inputs_dir="inputs", library_dir="library", outputs_dir="outputs", metadata_dir="metadata", sfx_dir="sfx"):
        self.inputs_dir = Path(inputs_dir)
        self.library_dir = Path(library_dir)
        self.outputs_dir = Path(outputs_dir)
        self.sfx_dir = Path(sfx_dir)
        self.metadata_cache = Path(metadata_dir) / "audio_index.json"
        self.audio_index = {}
        
        # Ensure directories exist
        for d in [self.inputs_dir, self.library_dir, self.outputs_dir, self.sfx_dir, Path(metadata_dir)]:
            d.mkdir(parents=True, exist_ok=True)

    def get_duration(self, file_path):
        """Get duration of a media file using ffprobe."""
        cmd = [
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', str(file_path)
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return float(result.stdout.strip())
        except (subprocess.CalledProcessError, ValueError):
            return None

    def index_library(self):
        """Index all audio files in the library folder."""
        print(f"Indexing library: {self.library_dir}...")
        new_index = {}
        extensions = ('.mp3', '.wav', '.m4a', '.aac', '.ogg')
        for audio_file in self.library_dir.glob('**/*'):
            if audio_file.suffix.lower() in extensions:
                duration = self.get_duration(audio_file)
                if duration:
                    new_index[str(audio_file)] = duration
        
        self.audio_index = new_index
        with open(self.metadata_cache, 'w') as f:
            json.dump(self.audio_index, f, indent=4)
        print(f"Indexed {len(self.audio_index)} audio tracks.")

    def find_matches(self, target_duration, tolerance=1.0, count=2):
        """Find audio tracks that match the target duration within tolerance."""
        if not self.audio_index:
            self.index_library()

        matches = [
            path for path, dur in self.audio_index.items() 
            if abs(dur - target_duration) <= tolerance
        ]

        if len(matches) < count:
            longer_tracks = [
                path for path, dur in self.audio_index.items()
                if dur > target_duration and path not in matches
            ]
            matches.extend(random.sample(longer_tracks, min(len(longer_tracks), count - len(matches))))

        random.shuffle(matches)
        return matches[:count]

    def has_audio(self, file_path):
        """Check if a file has an audio stream."""
        cmd = [
            'ffprobe', '-v', 'error', '-select_streams', 'a', 
            '-show_entries', 'stream=index', '-of', 'csv=p=0', str(file_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return bool(result.stdout.strip())

    def mix_audio(self, video_path, audio_path, output_path, style="balanced", bgm_volume=0.15):
        """Mix background audio into video with style-based processing."""
        video_duration = self.get_duration(video_path)
        if not video_duration: return

        has_orig = self.has_audio(video_path)
        
        # Base BGM processing: Trim + Fade
        bgm_chain = f"[1:a]atrim=0:{video_duration},afade=t=out:st={video_duration-1}:d=1,volume={bgm_volume}[bgm_clean]"
        
        # If SFX requested and available, overlay random SFX
        sfx_files = list(self.sfx_dir.glob("*.mp3")) + list(self.sfx_dir.glob("*.wav"))
        sfx_overlay = ""
        final_bgm = "[bgm_clean]"
        
        if style == "impact" and sfx_files:
            sfx = random.choice(sfx_files)
            # Use amovie for audio files. Escape path for ffmpeg filters.
            sfx_path = str(sfx).replace('\\', '/').replace(':', '\\:')
            sfx_overlay = f";amovie='{sfx_path}'[sfx];[bgm_clean][sfx]amix=inputs=2:duration=first[bgm_impact]"
            final_bgm = "[bgm_impact]"

        if has_orig:
            filter_complex = f"{bgm_chain}{sfx_overlay};[0:a]{final_bgm}amix=inputs=2:duration=first[a]"
        else:
            filter_complex = f"{bgm_chain}{sfx_overlay};{final_bgm}asplit=1[a]"

        cmd = [
            'ffmpeg', '-y',
            '-i', str(video_path),
            '-i', str(audio_path),
            '-filter_complex', filter_complex,
            '-map', '0:v', '-map', '[a]',
            '-c:v', 'copy', '-c:a', 'aac', '-shortest',
            str(output_path)
        ]

        print(f"[{style}] Processing: {video_path.name} with {Path(audio_path).name}...")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Error processing {video_path.name}: {result.stderr}")

    def run(self, versions_per_video=2, styles=["balanced", "impact"]):
        if not self.audio_index:
            self.index_library()

        video_extensions = ('.mp4', '.mov', '.mkv', '.avi')
        for video_file in self.inputs_dir.glob('*'):
            if video_file.suffix.lower() in video_extensions:
                duration = self.get_duration(video_file)
                if not duration: continue
                
                matches = self.find_matches(duration, count=versions_per_video)
                for i, audio_match in enumerate(matches):
                    style = styles[i % len(styles)]
                    output_name = f"{video_file.stem}_{style}_v{i+1}{video_file.suffix}"
                    output_path = self.outputs_dir / output_name
                    self.mix_audio(video_file, audio_match, output_path, style=style)

if __name__ == "__main__":
    mixer = VideoMixer()
    mixer.run()
