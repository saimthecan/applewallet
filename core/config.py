from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    api_base_url: str = "http://localhost:8000"
    app_name: str = "Loyal Bear"
    app_domain: str = "loyalbear.co"

    apple_pass_type_id: str = ""
    apple_team_id: str = ""
    apple_pass_cert_p12_b64: str = ""
    apple_pass_cert_password: str = ""
    apple_wwdr_cert_b64: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
