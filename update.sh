#!/usr/bin/env bash
# UPMI-Scanner Update & Rebuild Utility
# Fetches the latest code from GitHub, sets permissions, and verifies dependencies.

# Define terminal colors for professional output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}      UPMI-Scanner Rebuild Utility      ${NC}"
echo -e "${CYAN}========================================${NC}"

# 1. Update Repository
echo -e "\n${YELLOW}[1/3] Fetching latest updates from GitHub...${NC}"
if git pull; then
    echo -e "${GREEN}✓ Repository successfully updated.${NC}"
else
    echo -e "${RED}✗ Failed to pull updates. Check your internet connection or git repository status.${NC}"
    exit 1
fi

# 2. Set Execution Permissions
echo -e "\n${YELLOW}[2/3] Setting execution permissions...${NC}"
# Automatically make all python scripts in the directory executable
chmod +x *.py
echo -e "${GREEN}✓ Python scripts are now executable.${NC}"

# 3. Verify System Dependencies
echo -e "\n${YELLOW}[3/3] Verifying system dependencies...${NC}"
MISSING_DEPS=0

# Check FFmpeg (Audio Recording)
if ! command -v ffmpeg &> /dev/null; then
    echo -e "${RED}✗ FFmpeg is missing (Required for MP3 VOX recording).${NC}"
    MISSING_DEPS=1
else
    echo -e "${GREEN}✓ FFmpeg is installed.${NC}"
fi

# Check Python 'sounddevice' module
if ! python3 -c "import sounddevice" &> /dev/null; then
    echo -e "${RED}✗ Python 'sounddevice' module is missing.${NC}"
    MISSING_DEPS=1
else
    echo -e "${GREEN}✓ python3-sounddevice is installed.${NC}"
fi

# Check Python 'numpy' module
if ! python3 -c "import numpy" &> /dev/null; then
    echo -e "${RED}✗ Python 'numpy' module is missing.${NC}"
    MISSING_DEPS=1
else
    echo -e "${GREEN}✓ python3-numpy is installed.${NC}"
fi

# Check native RTL-SDR C-Library
if ! ldconfig -p | grep -q librtlsdr; then
     echo -e "${YELLOW}! librtlsdr might be missing. Ensure 'rtl-sdr' is installed on your system.${NC}"
else
     echo -e "${GREEN}✓ librtlsdr native library found.${NC}"
fi

echo -e "\n${CYAN}========================================${NC}"
if [ $MISSING_DEPS -eq 1 ]; then
    echo -e "${RED}Update complete, but missing dependencies were detected!${NC}"
    echo -e "To ensure all features work, please install them via your package manager."
    echo -e "Example: ${YELLOW}sudo apt install ffmpeg python3-sounddevice python3-numpy rtl-sdr${NC}"
else
    echo -e "${GREEN}Update complete! All systems and dependencies are ready.${NC}"
fi
echo -e "${CYAN}========================================${NC}\n"
