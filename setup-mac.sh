#!/bin/bash
# catalyst-retest-lab Mac setup — run this once from the project directory
set -euo pipefail
cd "$(dirname "$0")"

echo "=== Step 1: Checking prerequisites ==="

# Homebrew
if ! command -v brew &>/dev/null; then
    echo "ERROR: Homebrew not found. Install it first:"
    echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    exit 1
fi
echo "  Homebrew: $(brew --version | head -1)"

# Python 3.12+
PYTHON_OK=false
for py in python3.13 python3.12 python3; do
    if command -v "$py" &>/dev/null; then
        ver=$("$py" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 12 ]; then
            echo "  Python: $("$py" --version)"
            PYTHON_OK=true
            break
        fi
    fi
done
if [ "$PYTHON_OK" = false ]; then
    echo "  Python 3.12+ not found, installing..."
    brew install python@3.12
    echo "  Python: $(python3.12 --version)"
fi

# uv
if ! command -v uv &>/dev/null; then
    echo "  uv not found, installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
echo "  uv: $(uv --version)"

# PostgreSQL
if ! command -v initdb &>/dev/null; then
    echo "  PostgreSQL not found, installing..."
    brew install postgresql@16
    # Add to PATH for this session
    export PATH="$(brew --prefix postgresql@16)/bin:$PATH"
    echo ""
    echo "  IMPORTANT: Add PostgreSQL to your shell PATH permanently:"
    echo "    echo 'export PATH=\"$(brew --prefix postgresql@16)/bin:\$PATH\"' >> ~/.zshrc"
    echo ""
fi
echo "  PostgreSQL: $(pg_config --version)"

echo ""
echo "=== Step 2: Syncing Python dependencies ==="
# The ./run script handles venv setup via uv
chmod +x run
./run python -c "import sys; print(f'  Python {sys.version}')"

echo ""
echo "=== Step 3: Initializing local database ==="
# Development cluster only. Owner ledgers are never initialized or migrated by setup.
DEV_DIR="$HOME/.local/share/catalyst-retest-lab/dev"
./run catalyst-lab dev-init --local-dir "$DEV_DIR"

echo ""
echo "=== Step 4: Verifying .env ==="
if [ -f .env ]; then
    echo "  .env file found"
    # Report presence only; never print a value or anything derived from it.
    while IFS='=' read -r key value; do
        [ -z "$key" ] && continue
        [[ "$key" =~ ^# ]] && continue
        if [ -n "$value" ]; then
            echo "  $key: present"
        else
            echo "  $key: EMPTY"
        fi
    done < .env
else
    echo "  WARNING: No .env file found"
fi

echo ""
echo "=== Step 5: Starting server ==="
echo "Database and dependencies are ready."
echo ""
echo "To start the server, run:"
echo "  cd $(pwd)"
echo "  ./run catalyst-lab serve --local-dir \"$DEV_DIR\""
echo ""
echo "The server will start at http://127.0.0.1:8765"
echo "Press Ctrl+C to stop it."
echo ""
echo "To stop the database later:"
echo "  ./run catalyst-lab dev-stop --local-dir \"$DEV_DIR\""
