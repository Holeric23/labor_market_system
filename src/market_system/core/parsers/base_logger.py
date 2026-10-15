from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

@dataclass
class ParserLogReport:
    """Структурированный отчет о работе парсера."""
    
    parser_name: str
    started_at: datetime
    finished_at: datetime
    statistics: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    
    def format(self) -> str:
        """Форматирование отчета в строку."""
        duration = self.finished_at - self.started_at
        
        lines = [
            f"{self.parser_name} Parser",
            "=" * 60,
            "",
            f"Started:  {self.started_at.isoformat()}",
            f"Finished: {self.finished_at.isoformat()}",
            f"Duration: {duration}",
            "",
            "STATISTICS",
            "-" * 60,
        ]
        
        for key, value in self.statistics.items():
            lines.append(f"{key:25} {value}")
        
        lines.extend([
            "",
            "ERRORS",
            "-" * 60,
        ])
        
        if self.errors:
            lines.extend(self.errors)
        else:
            lines.append("No errors.")
        
        lines.extend([
            "",
            "END OF LOG",
            "=" * 60,
        ])
        
        return "\n".join(lines)


def write_parser_log(
    log_directory: Path,
    report: ParserLogReport,
) -> None:
    """Запись структурированного отчета в файл."""
    log_directory.mkdir(parents=True, exist_ok=True)
    
    log_filename = (
        f"{report.parser_name}_"
        f"{report.started_at.strftime('%Y-%m-%d_%H-%M-%S')}"
        f".txt"
    )
    
    log_path = log_directory / log_filename
    log_path.write_text(report.format(), encoding="utf-8")