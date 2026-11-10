FROM python:3.12-slim
WORKDIR /app
COPY tracker.py config.yaml config-rare.yaml config-both.yaml ./
# state.json lives on a persistent volume in production; without one a
# redeploy just re-runs --bootstrap (harmless, a few minutes).
EXPOSE 8080
CMD ["python3", "tracker.py"]
