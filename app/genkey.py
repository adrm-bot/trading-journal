"""python -m app.genkey  →  APP_SECRET_KEY 용 Fernet 키 1개 출력."""
from cryptography.fernet import Fernet

if __name__ == "__main__":
    print(Fernet.generate_key().decode())
