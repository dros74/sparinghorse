FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY SparingHorse.py .
ENV SH_DB=/data/sparinghorse.db
EXPOSE 8770
# Production server (not Flask's dev server). Imports `app` from SparingHorse.py.
CMD ["waitress-serve", "--listen=0.0.0.0:8770", "SparingHorse:app"]
