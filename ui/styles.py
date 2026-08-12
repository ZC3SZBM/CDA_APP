# ui/styles.py
import streamlit as st

def load_global_css():
    st.markdown("""
    <style>
    /* =====================================================
       HEADER TEXT (TOP RIGHT)
       ===================================================== */
    .jd-top-right {
        font-size: 15px;
        font-weight: 900;
        color: #367C2B;   /* John Deere Green */
        white-space: nowrap;
        text-align: right;
        margin-top: 10px;
    }

    /* =====================================================
       BUTTON STYLING – LIGHT MODE (DEFAULT)
       ===================================================== */
    .stButton > button {
        background-color: #367C2B; /* JD Green */
        color: #FFFFFF;
        border-radius: 6px;
        font-weight: 600;
        border: none;
        padding: 10px 18px;
        transition: background-color 0.2s ease-in-out;
    }

    .stButton > button:hover,
    .stButton > button:focus,
    .stButton > button:active {
        background-color: #63BB30 !important; /* Hover color */
        color: #FFFFFF;
        outline: none;
        box-shadow: none;
    }

    /* =====================================================
       DARK MODE OVERRIDES
       ===================================================== */
    @media (prefers-color-scheme: dark) {

        .stButton > button {
            background-color: #2E6E1F; /* Dark-safe JD Green */
            color: #FFFFFF;
        }

        .stButton > button:hover,
        .stButton > button:focus,
        .stButton > button:active {
            background-color: #63BB30 !important;
            color: #000000; /* Better contrast on bright green */
        }

        .jd-top-right {
            color: #367C2B; /* Slightly brighter in dark mode */
        }
    }
    </style>
    """, unsafe_allow_html=True)
