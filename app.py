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
from engine.packing import pack_containers_exact

# ✅ IMPORT DOWNLOAD FUNCTION
from engine.container_report import add_download_buttons

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

btn_left, btn_spacer, btn_right = st.columns([1, 6, 1])

with btn_left:
    calculate_clicked = st.button("Calculate Loading")

with btn_right:
    reset_clicked = st.button("Reset")

if reset_clicked:
    st.rerun()

# ---------------------------------------------------------
# RUN
# ---------------------------------------------------------

if calculate_clicked:
    data = df_input[
        df_input["Rack / Finished Good"].astype(str).str.strip() != ""
    ]

    if data.empty:
        st.error("No valid rack data.")
        st.stop()

    # ✅ PACKING
    containers = pack_containers_exact(
        data,
        CONTAINERS[container_type],
    )

    # ✅ SHOW RESULTS
    render_results(
        containers,
        data,
        container_type,
        origin_city,
        destination_city,
        container_cost_df,
        dry_van_cost_df,
    )

    # ---------------------------------------------------------
    # ✅ DOWNLOAD PDF + PPT BUTTONS (FINAL CORRECT PLACEMENT)
    # ---------------------------------------------------------
    dims = data.set_index("Rack / Finished Good").to_dict("index")
    container_spec = CONTAINERS[container_type]

    add_download_buttons(
    st,
    containers,
    dims,
    container_spec,
    container_type=container_type)

# ---------------------------------------------------------
# FOOTER
# ---------------------------------------------------------

render_feedback()
render_footer()
