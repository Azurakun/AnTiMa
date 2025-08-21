# cogs/chess_cog.py
import discord
from discord.ext import commands
import logging
import chess
import chess.svg
import chess.engine
import cairosvg
import io

logger = logging.getLogger(__name__)

class ChessCog(commands.Cog, name="Chess"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.games = {}  # Stores ongoing games per channel {channel_id: game_data}
        self.engine_path = "stockfish" # Assumes stockfish is in the system PATH

    async def _render_and_send_board(self, channel: discord.TextChannel, board: chess.Board, content: str = ""):
        """Renders the board to a PNG and sends it to the channel."""
        # Generate SVG
        svg_board = chess.svg.board(board=board, size=400).encode("UTF-8")
        
        # Convert SVG to PNG in memory
        png_bytes = cairosvg.svg2png(bytestring=svg_board)
        png_file = io.BytesIO(png_bytes)
        png_file.seek(0)
        
        file = discord.File(png_file, filename="board.png")
        await channel.send(content=content, file=file)

    async def start_game(self, message: discord.Message, user_as_white: bool):
        """Starts a new chess game in the channel."""
        channel_id = message.channel.id
        if channel_id in self.games:
            await message.channel.send("a game is already in progress in this channel!")
            return

        try:
            engine = await chess.engine.SimpleEngine.popen_uci(self.engine_path)
            board = chess.Board()
            
            self.games[channel_id] = {
                "board": board,
                "engine": engine,
                "user_is_white": user_as_white
            }
            
            await message.channel.send(f"okay, let's play! you are **{'White' if user_as_white else 'Black'}**. good luck :D")

            if not user_as_white:
                # Bot is White, make the first move
                result = await engine.play(board, chess.engine.Limit(time=1.0))
                board.push(result.move)
                await self._render_and_send_board(message.channel, board, content=f"my move is **{result.move.uci()}**.")
            else:
                await self._render_and_send_board(message.channel, board, content="it's your turn to move.")

        except Exception as e:
            logger.error(f"Failed to start chess engine: {e}")
            await message.channel.send("i couldn't start my chess brain... maybe the admin needs to check my setup.")

    async def handle_user_move(self, message: discord.Message, move_str: str):
        """Handles a move made by a user."""
        channel_id = message.channel.id
        if channel_id not in self.games:
            # This shouldn't be triggered by the AI, but it's good practice
            return

        game = self.games[channel_id]
        board = game["board"]
        engine = game["engine"]

        try:
            move = board.parse_san(move_str)
        except ValueError:
            try:
                move = board.parse_uci(move_str)
            except ValueError:
                await message.channel.send(f"'{move_str}' isn't a valid move. try again using standard algebraic notation (e.g., `e4`, `Nf3`).")
                return
        
        board.push(move)

        if board.is_game_over():
            await self._render_and_send_board(message.channel, board, content=f"gg! the game is over. result: **{board.result()}**")
            await self.stop_game(message)
            return

        # Bot's turn
        await self._render_and_send_board(message.channel, board, content=f"you played **{move_str}**. nice move! let me think...")
        result = await engine.play(board, chess.engine.Limit(time=1.5))
        board.push(result.move)
        
        await self._render_and_send_board(message.channel, board, content=f"okay, my move is **{result.move.uci()}**.")

        if board.is_game_over():
            await message.channel.send(f"gg! the game is over. result: **{board.result()}**")
            await self.stop_game(message)

    async def stop_game(self, message: discord.Message):
        """Stops the game in the current channel."""
        channel_id = message.channel.id
        if channel_id in self.games:
            game = self.games.pop(channel_id)
            game["engine"].quit()
            await message.channel.send("game over! thanks for playing with me <3")
    
    def cog_unload(self):
        # Clean up any running engine processes when the cog unloads
        for game in self.games.values():
            game["engine"].quit()

async def setup(bot: commands.Bot):
    await bot.add_cog(ChessCog(bot))