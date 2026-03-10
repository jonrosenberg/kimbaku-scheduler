FROM python:3.12-slim

# Install Node.js 20 (required for Google Calendar MCP via npx)
RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-install the MCP package so npx doesn't fetch at runtime
RUN npx -y @cocal/google-calendar-mcp --version || true

COPY . .
RUN python scripts/init_db.py

CMD ["python", "bot/telegram_bot.py"]
