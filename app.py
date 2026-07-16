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

    # ---- Step 3: Process ----
    if st.button("Download & Rename Images", type="primary"):
        zip_buffer = io.BytesIO()
        failures = []
        success_count = 0
        seen_names = {}

        progress = st.progress(0.0)
        status = st.empty()
        done = 0

        headers = {"User-Agent": "Mozilla/5.0 (compatible; ImageDownloader/1.0)"}

        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for excel_row, master, mpn, images in valid_rows:
                for slot, url in images:
                    done += 1
                    progress.progress(done / total_imgs)
                    base = f"{mpn}_{master}_{mid_token}_{slot:02d}"
                    # avoid overwriting duplicate names
                    name = base
                    if name in seen_names:
                        seen_names[name] += 1
                        name = f"{base}_dup{seen_names[base]}"
                    else:
                        seen_names[name] = 0
                    fname = f"{name}.jpg"
                    status.text(f"Row {excel_row}: {fname}")

                    try:
                        resp = requests.get(url, headers=headers, timeout=30)
                        resp.raise_for_status()
                        img = Image.open(io.BytesIO(resp.content))
                        if img.mode != "RGB":
                            img = img.convert("RGB")
                        out = io.BytesIO()
                        img.save(out, format="JPEG", quality=95)
                        zf.writestr(fname, out.getvalue())
                        success_count += 1
                    except Exception as e:
                        failures.append((excel_row, fname, url, str(e)[:120]))

        progress.progress(1.0)
        status.text("Done.")

        st.success(f"Completed. {success_count} image(s) downloaded, {len(failures)} failed.")

        st.download_button(
            "⬇️ Download ZIP",
            data=zip_buffer.getvalue(),
            file_name=f"images_{mid_token}.zip",
            mime="application/zip",
        )

        # ---- Summary ----
        if failures:
            st.subheader("Failed downloads")
            st.dataframe(pd.DataFrame(failures, columns=["Excel Row", "Filename", "URL", "Error"]),
                         use_container_width=True, hide_index=True)
        if gap_rows:
            st.subheader("Skipped rows (validation)")
            st.dataframe(pd.DataFrame(gap_rows, columns=["Excel Row", "Reason"]),
                         use_container_width=True, hide_index=True)
