# ui/header.py
import streamlit as st
from pathlib import Path
from config.app_config import APP_TITLE


def render_header():
    base_path = Path(__file__).resolve().parent.parent

    jd_logo = base_path / "assets" / "john_deere_header.png"
    tool_logo = base_path / "assets" / "tool_logo.png"

    # Layout: JD logo | centered tool header | JD text
    col_left, col_center, col_right = st.columns([4, 6, 3])

    # LEFT: John Deere LOGO
    with col_left:
        if jd_logo.exists():
            st.image(str(jd_logo), width=150)

    # CENTER: Tool logo + Tool name (perfect alignment)
    with col_center:
        logo_col, title_col = st.columns([2, 19], gap="small")

        with logo_col:
            if tool_logo.exists():
                st.image(str(tool_logo), width=200)

        with title_col:
            st.markdown(
                f"""
                <div style="display:flex;align-items:center;height:45px;">
                    <span style="font-size:25px;font-weight:700;">
                        {APP_TITLE}
                    </span>
                </div>
                """,
                unsafe_allow_html=True,
            )

    #  RIGHT: JD text
    with col_right:
        st.markdown(
            "<div class='jd-top-right'>LOGISTICS ENGINEERING</div>",
            unsafe_allow_html=True,
        )

    #  Green divider bar
    st.markdown(
        "<hr style='border:none;height:4px;background-color:#367C2B;margin:-25px 0 10px 0;'>",
        unsafe_allow_html=True,
    )
