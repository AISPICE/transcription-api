from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    gcp_project_id: str
    gcp_region: str = "us-central1"

    firestore_collection: str = "jobs"

    assemblyai_api_key: str
    anthropic_api_key: str
    apify_api_token: str

    # Public API key clients (pipelines, iPhone Shortcut) send in X-API-Key.
    api_key: str
    # Separate secret only Cloud Tasks knows, used to lock down /internal/process.
    internal_task_key: str

    cloud_tasks_queue: str = "transcription-queue"
    cloud_tasks_location: str = "us-central1"
    # Full https://... base URL of this Cloud Run service, used to build the
    # Cloud Tasks callback target. Set after the first deploy.
    service_url: str

    upload_bucket: str

    class Config:
        env_file = ".env"


settings = Settings()
