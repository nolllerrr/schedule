from datetime import date

from schedule_bot.downloader import ScheduleDownloader


HTML = """
<h2>Расписание занятий и объявления:</h2>
<div class="content">
  <table>
    <tr><td>Корпус 1</td><td>Корпус 2</td></tr>
    <tr>
      <td><a href="/files/21.-23.09.2026.xlsx">21.-23.09.2026</a></td>
      <td><a href="/files/21.09.26-25.09.26.xlsx">21.-25.09.2026</a></td>
    </tr>
  </table>
</div>
"""


def test_downloader_selects_requested_building() -> None:
    first = ScheduleDownloader(
        "https://example.test/schedule/", "downloads", building=1
    ).find_latest_in_html(HTML)
    second = ScheduleDownloader(
        "https://example.test/schedule/", "downloads", building=2
    ).find_latest_in_html(HTML)

    assert first.filename == "21.-23.09.2026.xlsx"
    assert first.end_date == date(2026, 9, 23)
    assert second.filename == "21.09.26-25.09.26.xlsx"
    assert second.end_date == date(2026, 9, 25)

