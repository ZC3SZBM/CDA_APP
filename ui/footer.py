# ui/footer.py
import streamlit as st
from pathlib import Path
import base64

def image_to_base64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()

def render_footer():
    base_path = Path(__file__).resolve().parent.parent
    footer_logo = base_path / "assets" / "john_deere_footer.png"

    if not footer_logo.exists():
        return

    logo_base64 = image_to_base64(footer_logo)

    # Spacer before footer
    st.markdown("<div style='height:80px'></div>", unsafe_allow_html=True)

    # ✅ TRUE FULL-WIDTH BRAND FOOTER
    st.markdown(
        f"""
        <div style="
            width:100vw;
            margin-left:calc(-50vw + 50%);
            height:40px;
            background-color:#f0f2f0;
            background-image:url('data:image/png;base64,{logo_base64}');
            background-repeat:no-repeat;
            background-position:center;
            background-size:120px auto;
        ">
        </div>
        """,
        unsafe_allow_html=True
    )