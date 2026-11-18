from dataclasses import dataclass


@dataclass
class Batch:
    publications: list
    cursor: dict
    done: bool = False
