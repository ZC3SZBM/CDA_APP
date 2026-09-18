import streamlit as st
from ui.styles import load_global_css
from ui.header import render_header
from ui.footer import render_footer
from ui.feedback import render_feedback
from ui.sections import (
    render_download_template,
    render_upload_section,
    render_results,
    load_costing_data,
)

from config.containers import CONTAINERS
from engine.packing import (pack_containers_exact, pack_into_n_containers,
                            PackingInputError)

# ---------------------------------------------------------
# Page config
# ---------------------------------------------------------

st.set_page_config(
    page_title="SmartPack - Container Load Planner",
    layout="wide",
)

# ---------------------------------------------------------
# GLOBAL CSS
# ---------------------------------------------------------

st.markdown(
    """
    <style>
        div.block-container { padding-top: 1.2rem; }
        .stDownloadButton { margin-bottom: 0.1rem !important; }
        .stFileUploader { margin-top: 0.1rem !important; margin-bottom: 0.1rem !important; }
        .stMarkdown { margin-bottom: 0.15rem !important; }
        .stAlert { margin-top: 0.2rem !important; margin-bottom: 0.2rem !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

load_global_css()
render_header()

# ---------------------------------------------------------
# INPUT SECTIONS
# ---------------------------------------------------------

render_download_template()
df_input = render_upload_section()
container_cost_df, dry_van_cost_df = load_costing_data()

# ---------------------------------------------------------
# SELECTION ROW
# ---------------------------------------------------------

col1, col2, col3 = st.columns(3)
with col1:
    container_type = st.selectbox(
        "Container Type",
        list(CONTAINERS.keys()),
    )
if container_type == "53 Dry Van":
    city_df = dry_van_cost_df
else:
    city_df = container_cost_df
origin_options = sorted(city_df["Origin City"].unique())
destination_options = sorted(city_df["Destination City"].unique())
with col2:
    origin_city = st.selectbox("Origin City", origin_options)
with col3:
    destination_city = st.selectbox("Destination City", destination_options)

# ---------------------------------------------------------
# BUTTONS
# ---------------------------------------------------------

# ── Optional: ship only what fits in a fixed number of containers ──────────
# Planners often have a fixed number of trucks booked and want them filled as
# full as possible, with the remainder waiting for the next shipment.
_lim1, _lim2 = st.columns([1, 3])
with _lim1:
    limit_on = st.checkbox("Limit to a fixed number of containers")
with _lim2:
    max_containers = st.number_input(
        "How many containers are booked?", min_value=1, value=8, step=1,
        disabled=not limit_on,
        help=("Fills exactly this many containers as full as possible and lists "
              "the deliveries left behind for the next shipment."))

btn_left, btn_spacer, btn_right = st.columns([1, 6, 1])
with btn_left:
    calculate_clicked = st.button("Calculate Loading")

with btn_right:
    reset_clicked = st.button("Reset")

if reset_clicked:
    # clear any stored result so the page goes back to a clean state
    st.session_state.pop("smartpack_result", None)
    st.rerun()

# ---------------------------------------------------------
# RUN  (compute ONCE, then remember it)
# ---------------------------------------------------------
# Streamlit reruns the whole script on every click - including a download-
# button click. So we must NOT keep the results/downloads inside the
# `if calculate_clicked:` block, or they disappear after one download and the
# user has to press Calculate again. Instead: when Calculate is pressed we do
# all the heavy work once (pack + build the PDF) and store it in
# st.session_state; the results + BOTH download buttons are then rendered from
# that stored result on every rerun, so both downloads keep working.

if calculate_clicked:
    data = df_input[
        df_input["Rack / Finished Good"].astype(str).str.strip() != ""
    ]

    if data.empty:
        st.error("No valid rack data.")
        st.stop()

    # Packaging Material is mandatory (it decides stacking / weight capacity).
    if "Packaging Material" in data.columns:
        _mat = data["Packaging Material"].astype(str).str.strip().str.lower()
        if (_mat.isin(["", "nan", "none"])).any():
            st.error("Every rack needs a **Packaging Material** before calculating. "
                     "Please fill it in for all rows.")
            st.stop()

    # CAPACITY QUERY: if the user entered a SINGLE package and left Quantity
    # blank/0, they want "how many fit in one container?" rather than a plan for
    # a known quantity. Use 1 so the planner still runs, and the capacity box in
    # the results answers their real question.
    if len(data) == 1:
        try:
            _q = float(data.iloc[0].get("Quantity", 0) or 0)
        except (TypeError, ValueError):
            _q = 0
        if _q <= 0:
            data = data.copy()
            data.iloc[0, data.columns.get_loc("Quantity")] = 1

    container_spec = CONTAINERS[container_type]

    # One-time heavy work: PACK the containers only. The PDF layout report is
    # NOT built here (it is slow for big jobs and often unwanted) — it is
    # generated on demand from the "Layout Report (PDF)" button in the results.
    left_behind = {}
    with st.spinner("Calculating loading plan\u2026 please wait."):
        try:
            if limit_on:
                containers, _shipped, left_behind = pack_into_n_containers(
                    data, container_spec, int(max_containers))
            else:
                containers = pack_containers_exact(data, container_spec)
        except PackingInputError as e:
            st.error(str(e))
            st.stop()
        dims = data.set_index("Rack / Finished Good").to_dict("index")

    # A fresh calculation invalidates any previously generated PDF.
    st.session_state.pop("layout_pdf_bytes", None)

    # Remember everything so it survives the reruns caused by download clicks.
    st.session_state["smartpack_result"] = {
        "containers":       containers,
        "data":             data,
        "container_type":   container_type,
        "origin_city":      origin_city,
        "destination_city": destination_city,
        "dims":             dims,
        "left_behind":      left_behind,
    }
    if left_behind:
        st.success(f"Loading plan ready \u2014 {len(containers)} container(s) filled, "
                   f"{sum(left_behind.values())} delivery(ies) left for the next shipment.")
    else:
        st.success(f"Loading plan ready \u2014 {len(containers)} container(s). "
                   "Download the Excel below, or generate the PDF layout report.")

# ---------------------------------------------------------
# SHOW RESULTS + DOWNLOADS  (runs on every rerun if a result exists)
# ---------------------------------------------------------

result = st.session_state.get("smartpack_result")
if result is not None:
    # Summary box (containers required + Excel + on-demand PDF) then the
    # container-wise plan + utilisation. All downloads live in render_results.
    render_results(
        result["containers"],
        result["data"],
        result["container_type"],
        result["origin_city"],
        result["destination_city"],
        container_cost_df,
        dry_van_cost_df,
        result.get("left_behind"),
    )

# ---------------------------------------------------------
# FOOTER
# ---------------------------------------------------------

render_feedback()
render_footer()
