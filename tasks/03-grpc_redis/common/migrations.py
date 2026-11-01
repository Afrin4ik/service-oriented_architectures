from alembic import context


def run():
    connection = context.config.attributes["connection"]
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
