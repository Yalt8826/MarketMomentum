import typer

from mktmtm.config.logging import configure_logging

app = typer.Typer(
    name="mktmtm",
    help="MktMtm market research and ranking system.",
)


@app.callback()
def cli() -> None:
    """MktMtm market research and ranking system."""
    configure_logging()


@app.command()
def status() -> None:
    """Show system status."""
    typer.echo("MktMtm is running.")


def main() -> None:
    app()
