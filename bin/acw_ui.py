from __future__ import annotations

import re
import shutil

from acw_ids import is_thread_id
from acw_ids import short_thread_id


def status_window_pane_cell(window_label: str, current_pane: str, thread_id: str, runtime_id: str) -> str:
    line1 = f"{window_label}/{current_pane}" if current_pane else window_label
    lines = [line1]
    if is_thread_id(thread_id):
        lines.append(f"[dim]{short_thread_id(thread_id)}[/dim]")
    if runtime_id:
        lines.append(f"[dim]id={runtime_id}[/dim]")
    return "\n".join(lines)


def status_flex_widths(term_w: int, fixed_col_widths: list[int]) -> tuple[int, int]:
    fixed_total = sum(fixed_col_widths)
    flex_total = max(48, term_w - fixed_total - 19)
    msg_col_w = max(20, min(32, flex_total // 3))
    last_agent_col_w = max(32, min(72, flex_total - msg_col_w))
    return last_agent_col_w, msg_col_w


def clamp_visual_lines(text: str, max_lines: int, col_width: int) -> str:
    if col_width < 10:
        col_width = 40
    lines = text.split("\n")
    visual = 0
    kept: list[str] = []
    for line in lines:
        visual_lines = max(1, (len(line) + col_width - 1) // col_width) if line else 1
        if visual + visual_lines > max_lines:
            remaining = max_lines - visual
            if remaining > 0:
                kept.append(line[: remaining * col_width])
            result = "\n".join(kept)
            if len(result) < len(text):
                result = result.rstrip() + "…"
            return result
        kept.append(line)
        visual += visual_lines
    return text


def strip_rich_markup(text: str) -> str:
    return re.sub(r"\[/?[^\]]*\]", "", text)


def status_time_markup(time_cell: str) -> str:
    lines: list[str] = []
    for raw_line in time_cell.splitlines():
        line = raw_line.strip()
        if not line:
            lines.append(line)
            continue
        if line.startswith("a:"):
            lines.append(f"[dim]{line}[/dim]")
            continue
        if len(line) >= 2 and line[1] == ":":
            lines.append(f"[dim]{line[:2]}[/dim]{line[2:]}")
            continue
        lines.append(line)
    return "\n".join(lines)


def status_table_plain(
    rows_data: list[tuple[str, ...]],
    *,
    section_every: int,
) -> None:
    headers = ("WINDOW/PANE", "STATE", "TIME", "LAST AGENT MESSAGE", "CONTINUE MESSAGE")
    plain_rows: list[tuple[str, str, str, str, str]] = []
    for row in rows_data:
        plain_rows.append(
            tuple(
                strip_rich_markup(cell).replace("\n", " / ")
                for cell in _normalize_status_row(row)
            )
        )

    widths = [len(header) for header in headers]
    for row in plain_rows:
        for idx, cell in enumerate(row):
            widths[idx] = min(max(widths[idx], len(cell)), 60)

    def _fmt(cell: str, width: int) -> str:
        if len(cell) > width:
            return cell[: width - 1] + "…"
        return cell.ljust(width)

    print("  ".join(_fmt(header, widths[idx]) for idx, header in enumerate(headers)))
    print("  ".join("-" * widths[idx] for idx in range(len(headers))))
    separator = "  ".join("-" * widths[idx] for idx in range(len(headers)))
    for idx, row in enumerate(plain_rows, 1):
        print("  ".join(_fmt(cell, widths[col_idx]) for col_idx, cell in enumerate(row)))
        if idx % section_every == 0 and idx != len(plain_rows):
            print(separator)


def render_status_table(
    rows_data: list[tuple[str, ...]],
    *,
    section_every: int,
    max_msg_lines: int,
) -> None:
    try:
        from rich.console import Console
    except ModuleNotFoundError:
        status_table_plain(rows_data, section_every=section_every)
        return
    Console().print(
        build_status_table_renderable(
            rows_data,
            section_every=section_every,
            max_msg_lines=max_msg_lines,
        )
    )


def build_status_table_renderable(
    rows_data: list[tuple[str, ...]],
    *,
    section_every: int,
    max_msg_lines: int,
):
    from rich.table import Table
    from rich.text import Text

    term_w = shutil.get_terminal_size((120, 24)).columns
    headers = ("WINDOW/PANE", "STATE", "TIME")
    col_widths = [len(header) for header in headers]
    for row in rows_data:
        normalized = _normalize_status_row(row)
        for idx in range(3):
            text = strip_rich_markup(normalized[idx].split("\n")[0])
            col_widths[idx] = max(col_widths[idx], len(text))
    last_agent_col_w, msg_col_w = status_flex_widths(term_w, col_widths)

    table = Table(show_lines=False, expand=True)
    table.add_column("WINDOW/PANE", no_wrap=True)
    table.add_column("STATE", no_wrap=True)
    table.add_column("TIME", no_wrap=True)
    table.add_column("LAST AGENT MESSAGE", ratio=2, max_width=last_agent_col_w)
    table.add_column("CONTINUE MESSAGE", ratio=1, max_width=msg_col_w)

    for idx, row in enumerate(rows_data, 1):
        window_pane, state_value, time_cell, last_agent, msg_raw = _normalize_status_row(row)
        table.add_row(
            Text.from_markup(window_pane),
            Text.from_markup(state_value),
            Text.from_markup(status_time_markup(time_cell)),
            Text.from_markup(clamp_visual_lines(last_agent, max_msg_lines, last_agent_col_w)),
            Text.from_markup(clamp_visual_lines(msg_raw, max_msg_lines, msg_col_w)),
        )
        if idx % section_every == 0 and idx != len(rows_data):
            table.add_section()

    return table


_WATCH_HEADER_TEXT = "Ctrl-C: exit; e: edit; s: start; p: pause; u: resume; r: restart"


def watch_header_text() -> str:
    return _WATCH_HEADER_TEXT


def build_status_watch_renderable(
    rows_data: list[tuple[str, ...]],
    *,
    section_every: int,
    max_msg_lines: int,
    recommendations: list[str],
    invariant_messages: list[str],
):
    from rich.console import Group
    from rich.text import Text

    renderables: list[object] = [
        Text(watch_header_text(), style="bold cyan"),
    ]
    if rows_data:
        renderables.append(
            build_status_table_renderable(
                rows_data,
                section_every=section_every,
                max_msg_lines=max_msg_lines,
            )
        )
    else:
        renderables.append(Text("(none)", style="dim"))

    for command in recommendations:
        renderables.append(Text(f"Recommendation: run {command}", style="bold green"))
    for message in invariant_messages:
        style = "yellow"
        if message.startswith("Shared daemon error") or message.startswith("Invariant error"):
            style = "bold red"
        elif message.startswith("Hint:"):
            style = "yellow"
        renderables.append(Text(message, style=style))

    return Group(*renderables)


def _normalize_status_row(row: tuple[str, ...]) -> tuple[str, str, str, str, str]:
    if len(row) == 5:
        return row[0], row[1], row[2], row[3], row[4]
    if len(row) == 6:
        return row[0], row[1], f"s: {row[2]}\nl: {row[3]}\na: -", row[4], row[5]
    raise ValueError(f"expected 5 or 6 columns in status row, got {len(row)}")


def print_doctor_plain(report, *, state_dir: str, tmux_socket: str) -> None:
    print("Doctor")
    print(f"  state_dir: {state_dir}")
    print(f"  tmux_socket: {tmux_socket}")
    for level, message in report.checks:
        print(f"[{level}] {message}")
    if report.recommendations:
        print()
        for cmd in report.recommendations:
            print(f"Recommendation: run {cmd}")
    print(f"RESULT: {report.result}")


def print_doctor_rich(report, *, state_dir: str, tmux_socket: str, result_styles: dict[str, str]) -> None:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()
    console.print(
        Panel.fit(
            f"state_dir: {state_dir}\n"
            f"tmux_socket: {tmux_socket}",
            title="Doctor",
        )
    )

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("status", no_wrap=True)
    table.add_column("message")
    for level, message in report.checks:
        style = result_styles.get(level, "")
        label = level.upper().ljust(9)
        table.add_row(f"[{style}]{label}[/{style}]" if style else label, message)
    console.print(table)

    if report.recommendations:
        console.print()
        for cmd in report.recommendations:
            console.print(f"Recommendation: run [bold]{cmd}[/bold]")

    result_style = result_styles.get(report.result, "")
    result_text = f"[{result_style}]RESULT: {report.result}[/{result_style}]" if result_style else f"RESULT: {report.result}"
    console.print(result_text)
