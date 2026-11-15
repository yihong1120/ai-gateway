"""Read-boundary default for tests focused on other EIP responsibilities."""


async def new_calendar_matches(lines):
    return [{'status': 'new'} for _ in lines]
