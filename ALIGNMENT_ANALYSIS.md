# Subtitle alignment review

Run `python3 scripts/analyze_alignment.py` to analyze current completed transcripts against the latest locally downloaded subtitle tracks. The reports in `alignment-analysis/` are ignored by Git. `report.md` is a review table; `report.json` includes the subtitle hash, sample counts, offset dispersion, estimated drift and example pairs of Whisper text and subtitle text. The two movies explicitly excluded from the first run (Cool Runnings and Wish Upon a Star) are skipped by this script.

This is a deterministic text-matching heuristic, not an AI model or a calibrated probability. It searches for similar phrases (allowing some recognition mistakes) over offsets within ten minutes, finds a consistent timing offset across sampled windows, and reports a composite 0–100 fit score based on matching coverage and text similarity. It does not measure actual subtitle quality outside sampled minutes. A positive offset means move the subtitle track later; a negative offset means earlier. Whisper timing and cue start differences introduce uncertainty, and different releases, missing scenes, or frame rates may need a non-constant offset. Large offsets, low scores, few matching sample windows, and drift warnings all warrant extra attention. Always verify with playback before approving; no movie subtitles or offsets are changed by this script.

## Automatically try more subtitle candidates

With the app running and OpenSubtitles credentials configured, run:

```bash
.venv/bin/python -u scripts/auto_fit_subtitles.py
```

When run in a terminal, the script asks for a target fit score and a maximum number of downloaded tracks per movie. Press Enter to accept the defaults (96 and 10). The track limit includes tracks already downloaded for that movie. For each confirmed movie missing saved subtitles that has not been marked Pass, the script scores existing tracks, then tries untried candidates until it reaches your target or limit, or OpenSubtitles has no more matching candidates. It checks result pages until OpenSubtitles stops allowing deeper pagination, and does not redownload rejected or previously downloaded candidates. If the provider limits a search before all results can be checked, it tries an ID-based search where available, marks that movie as incomplete, and continues with the next movie. Previously downloaded tracks count toward the limit; locally created trim/split tracks do not. Scores and recommended offsets are updated in the existing reports and Preview, but no subtitle is approved automatically.

The program needs a completed sampled transcription to determine the fit, so it skips movies without one. It also preserves the earlier exclusion of Cool Runnings and Wish Upon a Star. Run the transcription script first for other missing movies if needed. The score is a heuristic and a score of 96 is not proof of synchronization; review before approving.

To preview the queue without searching or downloading, use `--dry-run` (it refreshes the local score report). To run only a particular movie, add `--title 'Exact movie title'` (repeat the option for more). `--max-new-downloads 5` caps download attempts in a run; `--target` and `--max-tracks` supply values without prompting; non-interactive runs use the defaults for any omitted values. The script stops on API or quota errors, and you can rerun it to continue. An interrupted or uncertain download is recorded in `data/auto-fit-progress.json` and is not retried automatically, to avoid spending the allowance twice. The last-run summary is stored in `data/auto-fit-last-run.json`.

Broad searches move on after two consecutive pages with no matching movie titles and try an IMDb-based search when available. Such results are marked incomplete because later pages might contain a match. Use `--deep-search` to keep looking until OpenSubtitles’ pagination limit instead.
