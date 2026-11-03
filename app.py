"""A small Streamlit interface for the first Rescue Story Agent milestone."""

import streamlit as st


st.set_page_config(page_title="Rescue Story Agent", page_icon="🐾", layout="centered")


def clear_outputs():
    """Remove old results when the animal history changes."""
    st.session_state.pop("website_text", None)
    st.session_state.pop("facebook_text", None)


st.title("Rescue Story Agent")
st.subheader("AI-powered animal adoption content creator")
st.info("Milestone 1: a working demo. The buttons show placeholders; no AI is connected.")
st.write("Paste an animal's rescue history below, then choose the text you want to preview.")

history = st.text_area(
    "Animal History",
    height=220,
    placeholder="For example: Luna was rescued from a shelter and is now in foster care...",
    help="You can write in any language. This demo does not translate or generate text yet.",
    key="animal_history",
    on_change=clear_outputs,
)

website_column, facebook_column = st.columns(2)

with website_column:
    website_clicked = st.button("Generate Website Text", width="stretch", type="primary")

with facebook_column:
    facebook_clicked = st.button("Generate Facebook Text", width="stretch")

if website_clicked or facebook_clicked:
    if not history.strip():
        st.warning("Please enter an animal history first.")
    else:
        if website_clicked:
            st.session_state.website_text = (
                "Voorbeeldtekst voor de website: hier verschijnt later een Nederlandse "
                "adoptiebeschrijving op basis van de ingevoerde geschiedenis."
            )
        if facebook_clicked:
            st.session_state.facebook_text = (
                "Voorbeeldtekst voor Facebook: hier verschijnt later een kort Nederlands "
                "adoptiebericht op basis van de ingevoerde geschiedenis. 🐾"
            )

if "website_text" in st.session_state:
    st.divider()
    st.subheader("Website Text")
    st.caption("Placeholder preview — this is sample text, not an AI-generated adoption story.")
    st.write(st.session_state.website_text)

if "facebook_text" in st.session_state:
    st.divider()
    st.subheader("Facebook Text")
    st.caption("Placeholder preview — this is sample text, not an AI-generated adoption story.")
    st.write(st.session_state.facebook_text)

st.caption("No Google Drive, AI API, or account is needed. Your history is not saved to a file.")
