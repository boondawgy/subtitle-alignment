# Overnight movie transcription

This standalone script samples the cached list of movies missing subtitles. It transcribes the first 60 seconds, skips 300 seconds, then repeats (samples begin at 0:00, 6:00, 12:00, and so on). It does not approve or save movie subtitles and does not call OpenSubtitles. Movies whose subtitle checks have not finished are skipped until the library scan completes.

## Setup on the Mac

Mount the `Media` SMB share in Finder. The default movie path is `/Volumes/Media/Movies`; use `--movie-root` if yours is mounted elsewhere. Install FFmpeg (`brew install ffmpeg`) and Python 3.12 if needed. From the project directory:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-transcription.txt
python scripts/transcribe_missing.py --list
```

The first transcription run downloads the `tiny.en` model; it needs an internet connection and some disk space. Then run:

```bash
python -u scripts/transcribe_missing.py 2>&1 | tee -a transcriptions/run.log
```

Keep the Mac awake and the share mounted. Close the laptop only if it is configured to stay awake. The process runs until all queued movies are processed or interrupted. Press Ctrl-C to stop; rerun the same command to resume completed samples. Failed movies are reported and the script continues. Run the command again to retry failures. `--limit 1` is useful for a trial run; `--model base.en` gives a larger, slower model. Changing the model or sampling parameters requires moving the existing movie JSON aside so different settings are not mixed.

## Movie order

The script uses `scripts/movie_ratings.csv` and any `rating` or `mpaa_rating` fields in the cached movie records. It processes known PG-13 titles first, then known PG, then all others and unknowns alphabetically. The current library cache has no rating field, so the supplied CSV only covers movies whose ratings were checked in this project session. Add rows with the exact cached movie title and the MPAA rating to improve the ordering, or use `--ratings /path/to/ratings.csv`. The two-column CSV header is `title,rating`; quote titles containing commas.

## Output for future alignment

Each movie gets an atomic, resumable JSON file in `transcriptions/` (ignored by Git). `movie` identifies the file and its size, modification time, duration, and rating. `transcription` records model and sampling settings. Each completed `samples` entry records its movie-time range and absolute start/end times for segments and recognized words, along with Whisper confidence indicators. `subtitle_tracks` contains the locally available downloaded SRT text, candidate name and ID, and SHA-256 digest. Tracks are refreshed when the movie is processed again; rerun after downloading additional subtitle candidates to snapshot them. Current playback offsets are not baked into SRT timestamps.

A later alignment tool can compare recognized speech against the subtitle text, estimate an offset from timestamp differences, and assign its own confidence. This script does not calculate alignment or guarantee transcription accuracy. The tiny model is quick but can mishear dialogue, especially with music or noise.
