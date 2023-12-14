"""
Google Drive integration for puzzle organization

This is a separate cog so that Google Drive integration
can be easily disabled; simply omit this file.
"""

import datetime
import logging
import string
from typing import Optional

import discord
from discord.ext import commands, tasks
import gspread_asyncio
import gspread_formatting
import pytz

from bot.utils import urls
from bot.utils.puzzles_db import MissingPuzzleError, PuzzleDb
from bot.utils.gdrive import get_or_create_folder, rename_file
from bot.utils.gsheet import create_spreadsheet, get_manager
from bot.utils.gsheet_nexus import update_nexus
from bot import database
from bot.database.models import HuntSettings, PuzzleData

logger = logging.getLogger(__name__)


class GoogleSheets(commands.Cog):
    agcm = get_manager()

    def __init__(self, bot):
        self.bot = bot

    def begin_loops(self):
        logger.info("Beginning loops")
        self.refresh_nexus.start()

    @commands.Cog.listener()
    async def on_ready(self):
        print(f"{type(self).__name__} Cog ready.")

    def cap_name(self, name):
        """Capitalize name for easy comprehension"""
        return string.capwords(name.replace("-", " "))

    async def create_puzzle_spreadsheet(self, text_channel: discord.TextChannel, puzzle: PuzzleData):
        guild_id = text_channel.guild.id
        name = self.cap_name(puzzle.name)
        round_name = self.cap_name(puzzle.round_name)
        if name == "meta":
            # Distinguish metas between different rounds
            name = f"{name} ({round_name})"

        settings = await database.query_hunt_settings(guild_id)
        if not settings.drive_parent_id:
            return

        try:
            # create drive folder if needed
            round_folder = await get_or_create_folder(
                name=round_name, parent_id=settings.drive_parent_id
            )
            round_folder_id = round_folder["id"]

            spreadsheet = await create_spreadsheet(agcm=self.agcm, title=name, folder_id=round_folder_id)
            await puzzle.update(
                google_folder_id = round_folder_id,
                google_sheet_id = spreadsheet.id
            ).apply()

            # inform spreadsheet creation
            puzzle_url = puzzle.hunt_url
            sheet_url = urls.spreadsheet_url(spreadsheet.id)
            settings = await database.query_hunt_settings(guild_id)
            emoji = settings.discord_bot_emoji
            embed = discord.Embed(
                description=
                f"{emoji} I've created a spreadsheet for you at {sheet_url}. "
                f"Check out the `Quick Links` tab for more info! "
                # NOTE: This next sentence might be better elsewhere, for now easy enough to add message here.
                f"I've assumed the puzzle page is {puzzle_url}, use `!link` to update if needed."
            )
            await text_channel.send(embed=embed)

            # add some helpful links
            await self.add_quick_links_worksheet(spreadsheet, puzzle, settings)

        except Exception as exc:
            logger.exception(f"Unable to create spreadsheet for {round_name}/{name}")
            await text_channel.send(f":exclamation: Unable to create spreadsheet for {round_name}/{name}: {exc}")
            return

        return spreadsheet

    def update_cell_row(self, cell_range, row: int, key: str, value: str):
        """Update key-value row cell contents; row starts from 1"""
        cell_range[(row - 1) * 2].value = key
        cell_range[(row - 1) * 2 + 1].value = value

    async def add_quick_links_worksheet(
        self, spreadsheet: gspread_asyncio.AsyncioGspreadSpreadsheet, puzzle: PuzzleData, settings: HuntSettings
    ):
        worksheet = await spreadsheet.add_worksheet(title="Quick Links", rows=10, cols=2)
        cell_range = await worksheet.range(1, 1, 10, 2)

        self.update_cell_row(cell_range, 1, "Hunt URL", puzzle.hunt_url)
        self.update_cell_row(cell_range, 2, "Drive folder", urls.drive_folder_url(puzzle.google_folder_id))
        nexus_url = urls.spreadsheet_url(settings.drive_nexus_sheet_id) if settings.drive_nexus_sheet_id else ""
        self.update_cell_row(cell_range, 3, "Nexus", nexus_url)
        resources_url = urls.docs_url(settings.drive_resources_id) if settings.drive_resources_id else ""
        self.update_cell_row(cell_range, 4, "Resources", resources_url)
        self.update_cell_row(cell_range, 5, "Discord channel mention", puzzle.channel_mention)
        self.update_cell_row(
            cell_range, 6, "Reminders", "Please create a new worksheet if you're making large changes (e.g. re-sorting)"
        )
        self.update_cell_row(cell_range, 7, "", "You can use Ctrl+Alt+M to leave a comment on a cell")
        await worksheet.update_cells(cell_range)

        # Not async
        gspread_formatting.set_column_width(worksheet.ws, "B", 1000)

    async def archive_puzzle_spreadsheet(self, puzzle: PuzzleData) -> dict:
        def archive_puzzle_name(sheet_name):
            if "SOLVED" in sheet_name:
                return sheet_name
            return f"[SOLVED: {puzzle.solution}] {sheet_name}"

        return await rename_file(puzzle.google_sheet_id, name_lambda=archive_puzzle_name)

    @tasks.loop(seconds=60.0)
    async def refresh_nexus(self):
        """Ref: https://discordpy.readthedocs.io/en/latest/ext/tasks/"""
        for guild in self.bot.guilds:
            settings = await database.query_hunt_settings(guild.id)
            if settings.drive_nexus_sheet_id:
                puzzles = await PuzzleDb.get_all(guild.id)
                await update_nexus(agcm=self.agcm, file_id=settings.drive_nexus_sheet_id, puzzles=puzzles)

    @refresh_nexus.before_loop
    async def before_refreshing_nexus(self):
        await self.bot.wait_until_ready()
        logger.info("Ready to start updating nexus spreadsheet")


async def setup(bot):
    # Comment this out if google-drive-related package are not installed!
    await bot.add_cog(GoogleSheets(bot))

