"""strange_ringtone fetcher's runner: the only container that runs yt-dlp (docs/PLAN.md 6.1).

The fetcher image's third command (python -I /app/fetcher/runner.py, port
8080). It takes one validated video ID at a time from the fetcher over the
internal `jobs` network, runs yt-dlp and Deno, which reach YouTube only
through the forwarder, and hands the audio back once. Splitting it from the
fetcher keeps the code that parses YouTube's responses and runs its player
JavaScript out of the container the reverse proxy talks to.
"""
