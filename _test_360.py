import tempfile, logging
from pathlib import Path
from downloader import VideoDownloader, MAX_UPLOAD_BYTES
from config import settings
logging.basicConfig(level=logging.INFO)

url = "https://www.xvideos.com/video.omafhltca1b/eu_nao_me_importo_com_voce_mas_eu_quero_muito_que_voce_goze_em_mim."
d = VideoDownloader(settings)
with tempfile.TemporaryDirectory() as tmp:
    p, info = d._download_with_format(url, Path(tmp),
        "best[height<=360]/bestvideo[height<=360]+bestaudio", "360")
    print("RESULTADO:", p.name, p.stat().st_size, "limite", MAX_UPLOAD_BYTES,
          "cabe?", p.stat().st_size <= MAX_UPLOAD_BYTES)
