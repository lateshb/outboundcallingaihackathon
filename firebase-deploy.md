# Deployment Guide: Deploying Outbound Calling Platform to Firebase & Google Cloud Run

This guide explains how to deploy the Outbound Calling Platform. Due to system-level library dependencies for voice processing and the need for a persistent background agent worker, the app is deployed on **Google Cloud Run** and connected to **Firebase Hosting**.

---

## 🏗️ Architecture Overview

The system consists of three main components:
1. **Supabase Database**: Stores settings, logs, appointments, agent profiles, and campaigns.
2. **Google Cloud Run (Backend + Agent Worker)**:
   - Runs the FastAPI web server for dashboard REST endpoints and login management.
   - Runs the background LiveKit Agent worker (`agent.py`) which maintains a persistent WebSocket connection to the LiveKit server to handle outbound dialing and conversation loops.
3. **Firebase Hosting (Gateway)**: Routes web requests securely (SSL, custom domain) to the Cloud Run backend.

> [!IMPORTANT]
> **Why Cloud Run instead of Firebase Functions?**
> - **System Packages**: The LiveKit voice processing plugins require custom C libraries (`libsndfile1`, `libgomp1`, `libglib2.0-0`) installed via `apt-get` in the Dockerfile. Standard Firebase Functions cannot run custom apt packages.
> - **Persistent Connections**: The LiveKit agent worker is a long-running background daemon. Firebase Functions are event-driven, scale-to-zero, and have short request timeouts. They cannot host persistent websocket connections needed for VoIP.

---

## 🛠️ Prerequisites

1. **Google Cloud SDK**: Install and configure the [gcloud CLI](https://cloud.google.com/sdk/docs/install).
2. **Firebase CLI**: Install the [Firebase CLI](https://firebase.google.com/docs/cli).
3. **Docker**: Ensure Docker is installed and running on your local machine to build the container.
4. **Supabase Account**: Setup your database and run the `supabase_schema.sql` file in the SQL Editor.

---

## ⚙️ Step-by-Step Deployment

### Step 1: Initialize Firebase Project

If you haven't already, log in to Firebase and set up your project:
```bash
# Log in to Firebase CLI
firebase login

# Log in to Google Cloud CLI (ensure they use the same Google account)
gcloud auth login
gcloud auth configure-docker
```

Ensure `.firebaserc` points to your active Firebase project ID:
```json
{
  "projects": {
    "default": "your-firebase-project-id"
  }
}
```

---

### Step 2: Build and Push Docker Container to Google Artifact Registry

1. Create a repository in Google Artifact Registry (e.g., in `us-central1` named `outbound-calling`):
```bash
gcloud artifacts repositories create outbound-calling \
    --repository-format=docker \
    --location=us-central1 \
    --description="Outbound Calling Platform Container Registry"
```

2. Build the Docker container locally:
```bash
docker build -t us-central1-docker.pkg.dev/your-firebase-project-id/outbound-calling/app:latest .
```

3. Push the image to Artifact Registry:
```bash
docker push us-central1-docker.pkg.dev/your-firebase-project-id/outbound-calling/app:latest
```

---

### Step 3: Deploy Backend Container to Google Cloud Run

Deploy the container using the following command. It is **critical** to configure minimum instances and CPU allocation settings:

```bash
gcloud run deploy outbound-calling-backend \
    --image=us-central1-docker.pkg.dev/your-firebase-project-id/outbound-calling/app:latest \
    --region=us-central1 \
    --platform=managed \
    --allow-unauthenticated \
    --min-instances=1 \
    --no-cpu-throttling \
    --env-vars-file=.env
```

> [!WARNING]
> **Critical Cloud Run Flags Explained:**
> - `--min-instances=1`: Forces at least one instance to remain active. This ensures the LiveKit agent worker is constantly connected to LiveKit and ready to receive call requests instantly.
> - `--no-cpu-throttling` (or `--cpu=always`): Allocates full CPU resources outside of active HTTP requests. Without this, the background LiveKit worker will be starved of CPU cycles during voice calls, causing extreme audio lag, silence, or dropped calls.

---

### Step 4: Deploy to Firebase Hosting

Deploy the Firebase Hosting configuration which redirects traffic to the Cloud Run backend:
```bash
firebase deploy --only hosting
```

Your `firebase.json` is preconfigured to route all traffic securely to `outbound-calling-backend` Cloud Run service.

---

## 🔒 Post-Deployment Security & Setup

1. **Change Default Credentials**: Log in to the dashboard using the default credentials configured in your `.env` (e.g., `admin@example.com` / `adminpassword123`) and navigate to the **Users** tab to update or delete default users.
2. **Emergency Stop**: If you ever need to pause all calling functionality instantly, update the setting `EMERGENCY_STOP=true` in the **Settings** panel or redeploy with that environment variable.
