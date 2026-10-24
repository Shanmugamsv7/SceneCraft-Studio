FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FFMPEG_BIN=/usr/bin/ffmpeg \
    FFPROBE_BIN=/usr/bin/ffprobe

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fontconfig fonts-dejavu fonts-liberation libass9 \
    && test "$(command -v ffmpeg)" = "/usr/bin/ffmpeg" \
    && test "$(command -v ffprobe)" = "/usr/bin/ffprobe" \
    && /usr/bin/ffmpeg -version \
    && /usr/bin/ffprobe -version \
    && /usr/bin/ffmpeg -filters | grep -Eq '(^|[[:space:]])subtitles[[:space:]]+V->V' \
    && /usr/bin/ffmpeg -buildconf | grep -q -- '--enable-libass' \
    && test_dir="$(mktemp -d)" \
    && printf '1\\n00:00:00,000 --> 00:00:00,800\\nSceneCraft caption smoke test.\\n' > "${test_dir}/captions.srt" \
    && /usr/bin/ffmpeg -y -hide_banner -loglevel error -f lavfi -i color=c=black:s=320x180:r=30 -f lavfi -i sine=frequency=440:duration=1 -t 1 -c:v libx264 -pix_fmt yuv420p -c:a aac "${test_dir}/source.mp4" \
    && /usr/bin/ffmpeg -y -hide_banner -loglevel error -i "${test_dir}/source.mp4" -vf "subtitles=filename='captions.srt':charenc=UTF-8" -c:v libx264 -c:a copy "${test_dir}/captioned.mp4" \
    && test -s "${test_dir}/captioned.mp4" \
    && /usr/bin/ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of csv=p=0 "${test_dir}/captioned.mp4" | grep -q '^h264$' \
    && rm -rf "${test_dir}" \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN mkdir -p /app/data/jobs /app/data/footage-library \
    && useradd --system --uid 10001 --home-dir /app scenecraft \
    && chown -R scenecraft:scenecraft /app
USER scenecraft

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
