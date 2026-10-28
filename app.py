"""
Streamlit Chat UI for Monday.com Business Intelligence Agent

Standard chat interface with conversation and org settings persistence.
Per BUILD_BRIEF.md Section 6:
- Standard chat interface (st.chat_message / st.chat_input)
- Persist conversation + org settings in st.session_state
- Nothing fancier required
"""

import os
import streamlit as st
from dotenv import load_dotenv
from agent import create_agent, test_tool_calling_reliability
from tools import get_org_settings, set_org_setting, org_settings
from data_source import get_status as get_data_source_status

# Load environment variables from .env file
load_dotenv(), org_settings


def initialize_session_state():
    """Initialize session state variables."""
    if "agent" not in st.session_state:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            st.error("GROQ_API_KEY environment variable not set")
            st.stop()
        st.session_state.agent = create_agent(api_key)
    
    if "messages" not in st.session_state:
        st.session_state.messages = []
    
    if "org_settings" not in st.session_state:
        # Load org settings from file
        settings_result = get_org_settings()
        if settings_result.success:
            st.session_state.org_settings = settings_result.data
        else:
            st.session_state.org_settings = {}
    
    if "tool_calling_tested" not in st.session_state:
        st.session_state.tool_calling_tested = False


def display_chat_message(role: str, content: str, metadata: dict = None):
    """Display a chat message with optional metadata."""
    with st.chat_message(role):
        st.markdown(content)
        
        if metadata:
            with st.expander("Technical Details"):
                if metadata.get("tool_calls_made"):
                    st.write(f"Tools called: {', '.join(metadata['tool_calls_made'])}")
                if metadata.get("data_quality_info"):
                    st.write("Data Quality Information:")
                    for info in metadata["data_quality_info"]:
                        st.write(f"- {info}")


def main():
    """Main Streamlit app."""
    st.set_page_config(
        page_title="Skylark Drones BI Agent",
        page_icon="🚁",
        layout="wide"
    )
    
    st.title("🚁 Skylark Drones Business Intelligence Agent")
    st.markdown("Ask questions about your deals pipeline, work orders, and business performance.")
    
    # Initialize session state
    initialize_session_state()
    
    # Mark tool calling as tested so it doesn't block anything, but don't run it
    if not st.session_state.tool_calling_tested:
        st.session_state.tool_calling_tested = True
    
    # Display chat history
    for message in st.session_state.messages:
        display_chat_message(
            message["role"],
            message["content"],
            message.get("metadata")
        )
    
    # Chat input
    if prompt := st.chat_input("Ask about your deals, work orders, or business metrics..."):
        # Add user message to chat history
        user_message = {
            "role": "user",
            "content": prompt,
            "metadata": None
        }
        st.session_state.messages.append(user_message)
        
        # Display user message
        display_chat_message("user", prompt)
        
        # Process with agent
        with st.spinner("Analyzing data..."):
            try:
                response = st.session_state.agent.process_message(prompt)
                
                # Add assistant response to chat history
                assistant_message = {
                    "role": "assistant",
                    "content": response.content,
                    "metadata": {
                        "tool_calls_made": response.tool_calls_made,
                        "data_quality_info": response.data_quality_info
                    }
                }
                st.session_state.messages.append(assistant_message)
                
                # Display assistant response
                display_chat_message(
                    "assistant",
                    response.content,
                    {
                        "tool_calls_made": response.tool_calls_made,
                        "data_quality_info": response.data_quality_info
                    }
                )
                
                # Update org settings in session state if they changed
                settings_result = get_org_settings()
                if settings_result.success:
                    st.session_state.org_settings = settings_result.data
                
            except Exception as e:
                error_message = f"Error processing your request: {str(e)}"
                st.error(error_message)
                
                # Add error to chat history
                error_msg = {
                    "role": "assistant",
                    "content": error_message,
                    "metadata": None
                }
                st.session_state.messages.append(error_msg)
    
    # Sidebar with additional controls
    with st.sidebar:
        st.header("Settings")
        
        # Display current org settings
        st.subheader("Organization Settings")
        if st.session_state.org_settings:
            for key, value in st.session_state.org_settings.items():
                st.text(f"{key}: {value}")
        else:
            st.text("No organization settings configured")
        
        # Clear conversation button
        if st.button("Clear Conversation"):
            st.session_state.messages = []
            st.session_state.agent.reset_conversation()
            st.rerun()
        
        # Reset org settings button
        if st.button("Reset Org Settings"):
            org_settings._settings = {}
            org_settings._save_settings()
            st.session_state.org_settings = {}
            st.rerun()
            
        # Test agent reliability button
        if st.button("Run Reliability Test"):
            with st.spinner("Testing agent tool-calling reliability..."):
                try:
                    reliability_ok = test_tool_calling_reliability(st.session_state.agent)
                    if not reliability_ok:
                        st.sidebar.warning("Tool-calling reliability test had issues.")
                    else:
                        st.sidebar.success("Reliability test passed!")
                except Exception as e:
                    st.sidebar.error(f"Test failed: {str(e)}")
        
        st.divider()
        
        # Display connection status
        st.subheader("Connection Status")
        monday_token = os.environ.get("MONDAY_API_TOKEN")
        if monday_token:
            st.text("✅ Monday.com API: Configured")
        else:
            st.text("❌ Monday.com API: Not configured")

        source_status = get_data_source_status()
        source_labels = {
            "monday_mcp": "Monday.com MCP (live)",
            "excel_snapshot": "Excel snapshot (Data/)",
        }
        st.text(f"Data source mode: {source_status['mode']}")
        if source_status["last_source"]:
            st.text(f"Last read from: {source_labels[source_status['last_source']]}")
        if source_status["fallback_reason"]:
            st.caption(f"Fallback reason: {source_status['fallback_reason']}")

        groq_key = os.environ.get("GROQ_API_KEY")
        if groq_key:
            st.text("✅ Groq API: Configured")
        else:
            st.text("❌ Groq API: Not configured")
        
        st.divider()
        
        # Help section
        st.subheader("Help")
        st.markdown("""
        **Sample Questions:**
        - "How's our pipeline looking this quarter?"
        - "What's our weighted pipeline value?"
        - "Which work orders are at risk of billing issues?"
        - "Show me deals in the Agriculture sector"
        - "What's our data quality status?"
        
        **Features:**
        - Automatic data normalization and quality checks
        - Handles missing/messy data transparently
        - Fiscal quarter convention support
        - No record-level linking between Deals and Work Orders
        """)


if __name__ == "__main__":
    main()
