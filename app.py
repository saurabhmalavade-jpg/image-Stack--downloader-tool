import streamlit as st
import pandas as pd
import requests
import io
import zipfile
from PIL import Image

st.set_page_config(page_title="Image Downloader & Renamer", page_icon="🖼️", layout="centered")

# ---- Naming convention options ----
NAMING_CONVENTIONS = [
    "MPN_MasterID_ALG_PL_ISP_XX",
    "MPN_MasterID_ALGO_PL_ISP_XX",
    "MPN_MasterID_BSTB_US_ISP_XX",
    "MPN_MasterID_BSTB_CA_ISP_XX",
    "MPN_MasterID_BOL_NL_ISP_XX",
    "MPN_MasterID_BOL_BE_ISP_XX",
    "MPN_MasterID_EBY_US_ISP_XX",
    "MPN_MasterID_EBY_DE_ISP_XX",
    "MPN_MasterID_EBY_UK_ISP_XX",
    "MPN_MasterID_KHL_US_ISP_XX",
    "MPN_MasterID_LWS_US_ISP_XX",
    "MPN_MasterID_MCY_US_ISP_XX",
    "MPN_MasterID_MM_DE_ISP_XX",
    "MPN_MasterID_MLBR_US_ISP_XX",
    "MPN_MasterID_NRDM_US_ISP_XX",
    "MPN_MasterID_OCT_FR_ISP_XX",
    "MPN_MasterID_OTTO_DE_ISP_XX",
    "MPN_MasterID_TGT_US_ISP_XX",
    "MPN_MasterID_TSC_UK_ISP_XX",
    "MPN_MasterID_TTS_US_ISP_XX",
    "MPN_MasterID_TTS_UK_ISP_XX",
    "MPN_MasterID_WM_US_ISP_XX",
    "MPN_MasterID_WM_CA_ISP_XX",
    "MPN_MasterID_ZL_DE_ISP_XX",
    "MPN_MasterID_NOON_AE_ISP_XX",
    "MPN_MasterID_SPFY_US_ISP_XX",
]

st.title("🖼️ Image Downloader & Renamer")
st.caption("Download product images and rename them by marketplace naming convention.")

# ---- Step 1: Naming convention ----
st.subheader("Step 1 — Select naming convention")
convention = st.selectbox("Naming convention", NAMING_CONVENTIONS, index=6)

# The middle part (marketplace token), e.g. "EBY_US_ISP" from "MPN_MasterID_EBY_US_ISP_XX"
# Strip leading "MPN_MasterID_" and trailing "_XX"
mid_token = convention.replace("MPN_MasterID_", "").rsplit("_XX", 1)[0]
st.info(f"Filenames will look like:  `MPN_MasterID_{mid_token}_01.jpg`")

# ---- Step 2: Upload ----
st.subheader("Step 2 — Upload Excel file")
st.caption("Column A = Master ID, Column B = MPN, columns C onward = image URLs (in order).")
uploaded = st.file_uploader("Excel file", type=["xlsx", "xls"])


def is_url(val):
    if val is None:
        return False
    s = str(val).strip()
    return s.lower().startswith("http")


def is_blank(val):
    return val is None or str(val).strip() == "" or (isinstance(val, float) and pd.isna(val))


if uploaded is not None:
    try:
        df = pd.read_excel(uploaded, header=0, dtype=str)
    except Exception as e:
        st.error(f"Could not read the Excel file: {e}")
        st.stop()

    if df.shape[1] < 3:
        st.error("File must have at least 3 columns: Master ID, MPN, and one image URL column.")
        st.stop()

    cols = df.columns.tolist()
    master_col, mpn_col = cols[0], cols[1]
    url_cols = cols[2:]

    st.write(f"**{len(df)} rows** detected. Master ID = `{master_col}`, MPN = `{mpn_col}`, "
             f"{len(url_cols)} possible image columns.")

    # ---- Validation pass: detect gaps (blank between two URLs) ----
    gap_rows = []          # rows flagged and skipped
    valid_rows = []        # (excel_row_number, master, mpn, [(slot_index, url), ...])

    for idx, row in df.iterrows():
        excel_row = idx + 2  # +2: header row + 1-based
        master = row[master_col]
        mpn = row[mpn_col]

        if is_blank(master) or is_blank(mpn):
            gap_rows.append((excel_row, "Missing Master ID or MPN"))
            continue

        url_values = [row[c] for c in url_cols]
        # find last non-blank position
        last_filled = -1
        for i, v in enumerate(url_values):
            if not is_blank(v):
                last_filled = i
        # gap = any blank before the last filled position
        has_gap = any(is_blank(url_values[i]) for i in range(last_filled + 1)) if last_filled >= 0 else False
        # also flag non-URL text in a filled slot
        bad_url = any((not is_blank(v)) and (not is_url(v)) for v in url_values[:last_filled + 1])

        if has_gap:
            gap_rows.append((excel_row, "Gap between image URLs"))
            continue
        if bad_url:
            gap_rows.append((excel_row, "Non-URL value in image column"))
            continue
        if last_filled < 0:
            gap_rows.append((excel_row, "No image URLs"))
            continue

        images = [(i + 1, str(url_values[i]).strip()) for i in range(last_filled + 1)]
        valid_rows.append((excel_row, str(master).strip(), str(mpn).strip(), images))

    if gap_rows:
        st.warning(f"{len(gap_rows)} row(s) flagged and will be skipped (see details below).")
        with st.expander("Flagged rows"):
            st.dataframe(pd.DataFrame(gap_rows, columns=["Excel Row", "Reason"]),
                         use_container_width=True, hide_index=True)

    if not valid_rows:
        st.error("No valid rows to process.")
        st.stop()

    total_imgs = sum(len(imgs) for _, _, _, imgs in valid_rows)
    st.write(f"Ready to process **{len(valid_rows)} rows / {total_imgs} images**.")

    chunk_size = st.number_input(
        "Images per ZIP file",
        min_value=100, max_value=500, value=300, step=50,
        help="Each ZIP is served from memory on download, so it must stay small on Streamlit Cloud's "
             "~1 GB limit. At max quality, 300 per ZIP is safe. Lower this if downloads still fail.",
    )

    threads = st.slider(
        "Parallel downloads",
        min_value=4, max_value=32, value=16, step=4,
        help="Higher = faster, but some servers may rate-limit. 16 is a good balance.",
    )

    # ---- Step 3: Process ----
    if st.button("Download & Rename Images", type="primary"):
        import tempfile, os, glob
        from concurrent.futures import ThreadPoolExecutor, as_completed

        failures = []
        success_count = 0
        seen_names = {}

        progress = st.progress(0.0)
        status = st.empty()

        headers = {"User-Agent": "Mozilla/5.0 (compatible; ImageDownloader/1.0)"}

        # Work on disk, not in RAM
        work_dir = tempfile.mkdtemp(prefix="imgdl_")
        img_dir = os.path.join(work_dir, "images")
        zip_dir = os.path.join(work_dir, "zips")
        os.makedirs(img_dir, exist_ok=True)
        os.makedirs(zip_dir, exist_ok=True)

        # ---- Build the full job list first (assign unique filenames up front) ----
        jobs = []  # (excel_row, fname, url)
        for excel_row, master, mpn, images in valid_rows:
            for slot, url in images:
                base = f"{mpn}_{master}_{mid_token}_{slot:02d}"
                name = base
                if name in seen_names:
                    seen_names[base] += 1
                    name = f"{base}_dup{seen_names[base]}"
                else:
                    seen_names[name] = 0
                jobs.append((excel_row, f"{name}.jpg", url))

        # ---- Worker: download + save one image at maximum quality ----
        session = requests.Session()
        session.headers.update(headers)

        def fetch_one(job):
            excel_row, fname, url = job
            try:
                resp = session.get(url, timeout=60)
                resp.raise_for_status()
                img = Image.open(io.BytesIO(resp.content))
                # Preserve ICC profile if present for accurate color
                icc = img.info.get("icc_profile")
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                elif img.mode == "L":
                    img = img.convert("RGB")
                save_kwargs = dict(
                    format="JPEG",
                    quality=100,          # maximum quality
                    subsampling=0,        # 4:4:4, no chroma subsampling (sharpest)
                    optimize=True,
                    progressive=True,
                )
                if icc:
                    save_kwargs["icc_profile"] = icc
                img.save(os.path.join(img_dir, fname), **save_kwargs)
                return (True, excel_row, fname, url, None)
            except Exception as e:
                return (False, excel_row, fname, url, str(e)[:120])

        # ---- Run downloads in parallel ----
        done = 0
        with ThreadPoolExecutor(max_workers=threads) as ex:
            futures = [ex.submit(fetch_one, j) for j in jobs]
            for fut in as_completed(futures):
                ok, excel_row, fname, url, err = fut.result()
                done += 1
                if done % 5 == 0 or done == total_imgs:
                    progress.progress(done / total_imgs)
                    status.text(f"Downloaded {done}/{total_imgs} ...")
                if ok:
                    success_count += 1
                else:
                    failures.append((excel_row, fname, url, err))

        # ---- Pack into chunked ZIPs on disk ----
        status.text("Packing ZIP files...")
        all_files = sorted(glob.glob(os.path.join(img_dir, "*.jpg")))
        zip_paths = []
        num_chunks = (len(all_files) + chunk_size - 1) // max(1, chunk_size)
        for ci in range(num_chunks):
            part = all_files[ci * chunk_size:(ci + 1) * chunk_size]
            suffix = f"_part{ci + 1}of{num_chunks}" if num_chunks > 1 else ""
            zpath = os.path.join(zip_dir, f"images_{mid_token}{suffix}.zip")
            # ZIP_STORED: images are already compressed (JPEG); storing is faster and
            # avoids re-compression overhead / memory spikes during packing.
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as zf:
                for fp in part:
                    zf.write(fp, arcname=os.path.basename(fp))
            zip_paths.append(zpath)

        progress.progress(1.0)
        status.text("Done.")

        # Persist results so download buttons survive Streamlit reruns
        st.session_state["zip_paths"] = zip_paths
        st.session_state["num_chunks"] = num_chunks
        st.session_state["success_count"] = success_count
        st.session_state["failures"] = failures
        st.session_state["gap_rows"] = gap_rows

    # ---- Results (rendered from session_state so downloads don't re-trigger processing) ----
    if "zip_paths" in st.session_state and st.session_state["zip_paths"]:
        import os
        zip_paths = st.session_state["zip_paths"]
        num_chunks = st.session_state["num_chunks"]
        success_count = st.session_state["success_count"]
        failures = st.session_state["failures"]
        gap_rows = st.session_state.get("gap_rows", [])

        st.success(
            f"Completed. {success_count} image(s) downloaded, {len(failures)} failed. "
            f"Split into {len(zip_paths)} ZIP file(s)."
        )

        st.subheader("Download your files")
        if num_chunks > 1:
            st.caption("Batch split into multiple ZIPs — download each one separately.")

        # Each button reads its ZIP from disk ONLY when clicked (lazy callable),
        # and lives in a fragment so clicking it doesn't rerun the whole app.
        @st.fragment
        def zip_download(zpath):
            if not os.path.exists(zpath):
                st.error(f"ZIP no longer available (server restarted): "
                         f"{os.path.basename(zpath)}. Please re-run.")
                return
            size_mb = os.path.getsize(zpath) / (1024 * 1024)

            def load_bytes():
                with open(zpath, "rb") as fh:
                    return fh.read()

            st.download_button(
                f"⬇️ {os.path.basename(zpath)}  ({size_mb:.0f} MB)",
                data=load_bytes,               # callable -> read only on click
                file_name=os.path.basename(zpath),
                mime="application/zip",
                key=f"dl_{zpath}",
                on_click="ignore",             # don't rerun app on download
            )

        for zpath in zip_paths:
            zip_download(zpath)

        # ---- Summary ----
        if failures:
            st.subheader("Failed downloads")
            st.dataframe(pd.DataFrame(failures, columns=["Excel Row", "Filename", "URL", "Error"]),
                         use_container_width=True, hide_index=True)
        if gap_rows:
            st.subheader("Skipped rows (validation)")
            st.dataframe(pd.DataFrame(gap_rows, columns=["Excel Row", "Reason"]),
                         use_container_width=True, hide_index=True)
