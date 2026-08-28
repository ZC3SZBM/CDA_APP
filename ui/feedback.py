# ui/feedback.py
import streamlit as st
from config.app_config import PROJECT_OWNER, FEEDBACK_TEXT

def render_feedback():
    with st.expander("ℹ️ Project Ownership & Feedback"):
        st.markdown(f"""
        **Project Owner:** {PROJECT_OWNER['name']}  
        **Location:** {PROJECT_OWNER['location']}  
        **Contact:** {PROJECT_OWNER['email']}  

        {FEEDBACK_TEXT}
        """)