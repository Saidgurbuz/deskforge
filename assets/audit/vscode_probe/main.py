"""Small deterministic file for VS Code qualification runs."""


def greet(name: str) -> str:
    values = ["DeskShot", "ScreenParse", name]
    return " | ".join(values)


if __name__ == "__main__":
    print(greet("desktop-ui"))
