import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool
from sqlmodel import SQLModel

DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set")

print(f"[DB] Using DATABASE_URL = {DATABASE_URL.split('@')[0]}@***")

# Configuration robuste pour Supabase/PostgreSQL
engine = create_engine(
    DATABASE_URL,
    poolclass=QueuePool,
    pool_pre_ping=True,           # Vérifie la connexion avant chaque utilisation
    pool_recycle=280,             # Recycle les connexions avant timeout Supabase (5 min)
    pool_size=3,                  # Connexions maintenues ouvertes
    max_overflow=5,               # Connexions supplémentaires si nécessaire
    pool_timeout=30,              # Timeout pour obtenir une connexion
    echo=False,                   # Mettre True pour debug SQL
    connect_args={
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
        "connect_timeout": 30,
    }
)


# Event listener pour gérer les déconnexions
@event.listens_for(engine, "connect")
def connect(dbapi_connection, connection_record):
    connection_record.info["pid"] = os.getpid()


@event.listens_for(engine, "checkout")
def checkout(dbapi_connection, connection_record, connection_proxy):
    pid = os.getpid()
    if connection_record.info.get("pid") != pid:
        connection_record.dbapi_connection = None
        raise Exception("Connection belongs to different process")


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_session():
    with SessionLocal() as session:
        yield session

**Copiez ce code** et remplacez le contenu de `app/db/session.py` dans votre repo GitHub Render.

Après le déploiement, l'API devrait accepter les PUT sans erreurs 500! 🔧
