# Base must be a locally built, reviewed Deck Lab application image.
ARG DECK_LAB_IMAGE=deck-lab-preview-app:local
FROM ${DECK_LAB_IMAGE}
COPY seed_preview.py /opt/dispatcher/seed_preview.py
ENV DISPATCHER_PREVIEW=1 SABER_SKIP_DOTENV=1 SABER_DB_PATH=/tmp/deck-lab-preview.db \
    SABER_AUTH_MODE=password SABER_SECRET_KEY=disposable-local-preview-key \
    SABER_COOKIE_SECURE=0 SABER_DECK_LAB_REDESIGN=1 SABER_DECK_LAB_DEV=0 \
    SABER_RESEARCH_SYNC=0 SABER_PUBLIC=0 LINEAR_FEEDBACK_ENABLED=false
CMD ["python", "/opt/dispatcher/seed_preview.py"]
