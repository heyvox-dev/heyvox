#!/bin/bash
# HeyVox test suite runner
# Usage: ./scripts/run-tests.sh [unit|integration|e2e|stress|all]
#
# Levels:
#   unit        — Fast tests, no audio hardware needed (~5s)
#   integration — HUD IPC, flag coordination, adapters (~10s)
#   e2e         — Full pipeline via BlackHole (~60s)
#   stress      — Memory, rapid-fire, recovery via BlackHole (~5min)
#   all         — Everything

set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-/Users/work/.pyenv/versions/3.12.12/bin/python}"
PYTEST="$PYTHON -m pytest"
LEVEL="${1:-unit}"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}=== HeyVox Test Suite ===${NC}"
echo "Level: $LEVEL"
echo ""

run_tests() {
    local label="$1"
    shift
    echo -e "${YELLOW}--- $label ---${NC}"
    if $PYTEST "$@"; then
        echo -e "${GREEN}✓ $label passed${NC}"
    else
        echo -e "${RED}✗ $label failed${NC}"
        FAILED=1
    fi
    echo ""
}

FAILED=0

# Unit tests — always run
if [[ "$LEVEL" == "unit" || "$LEVEL" == "all" ]]; then
    run_tests "Unit: Config" tests/test_config.py -v --tb=short
    run_tests "Unit: Injection" tests/test_injection.py -v --tb=short
    run_tests "Unit: Wake Word Strip" tests/test_wake_word_strip.py -v --tb=short
    run_tests "Unit: Cues" tests/test_cues.py -v --tb=short
    run_tests "Unit: Adapters" tests/test_adapters.py -v --tb=short
fi

# Integration tests
if [[ "$LEVEL" == "integration" || "$LEVEL" == "all" ]]; then
    run_tests "Integration: HUD IPC" tests/test_hud_ipc.py -v --tb=short
    run_tests "Integration: Flag Coordination" tests/test_flag_coordination.py -v --tb=short
    run_tests "Integration: Media" tests/test_media.py -v --tb=short
    run_tests "Integration: Echo Suppression" tests/test_echo_suppression.py -v --tb=short
fi

# E2E tests — require BlackHole + running heyvox
if [[ "$LEVEL" == "e2e" || "$LEVEL" == "all" ]]; then
    # Check prerequisites
    if ! $PYTHON -c "import pyaudio; pa=pyaudio.PyAudio(); [exit(0) for i in range(pa.get_device_count()) if 'BlackHole' in pa.get_device_info_by_index(i)['name']]; exit(1)" 2>/dev/null; then
        echo -e "${RED}BlackHole not found — install with: brew install blackhole-2ch${NC}"
        echo "Then restart coreaudiod: sudo launchctl kickstart -kp system/com.apple.audio.coreaudiod"
        FAILED=1
    elif ! pgrep -f "heyvox.main" >/dev/null 2>&1; then
        echo -e "${RED}heyvox not running — start with BlackHole mic first${NC}"
        FAILED=1
    else
        run_tests "E2E: Pipeline" tests/test_e2e.py -v --tb=short -x
    fi
fi

# Stress tests — require BlackHole + running heyvox
if [[ "$LEVEL" == "stress" || "$LEVEL" == "all" ]]; then
    if ! pgrep -f "heyvox.main" >/dev/null 2>&1; then
        echo -e "${RED}heyvox not running — start with BlackHole mic first${NC}"
        FAILED=1
    else
        run_tests "Stress: Memory" tests/test_stress.py::TestMemoryStability -v -s --tb=short
        run_tests "Stress: Rapid Fire" tests/test_stress.py::TestRapidFire -v -s --tb=short
        run_tests "Stress: Flags" tests/test_stress.py::TestFlagCoordination -v -s --tb=short
        run_tests "Stress: Quality" tests/test_stress.py::TestTranscriptionQuality -v -s --tb=short
        run_tests "Stress: TTS Coord" tests/test_stress.py::TestTTSCoordination -v -s --tb=short
        run_tests "Stress: Timing" tests/test_stress.py::TestTimingRegression -v -s --tb=short
        run_tests "Stress: Recovery" tests/test_stress.py::TestErrorRecovery -v -s --tb=short
        run_tests "Stress: History" tests/test_stress.py::TestTranscriptHistory -v -s --tb=short
    fi
fi

echo ""
if [[ "$FAILED" -eq 0 ]]; then
    echo -e "${GREEN}=== All tests passed ===${NC}"
else
    echo -e "${RED}=== Some tests failed ===${NC}"
    exit 1
fi
