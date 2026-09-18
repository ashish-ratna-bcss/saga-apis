import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from osint_app.db import Base


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    from osint_app import models  # noqa: F401  (register tables)

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
