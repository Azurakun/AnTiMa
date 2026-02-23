

# --- DYNAMIC ACTION UI ---
class DynamicActionView(discord.ui.View):
    def __init__(self, engine: RPGEngine, channel: discord.Thread, user: discord.Member, actions: list[str]):
        super().__init__(timeout=300)  # 5 minute timeout for the buttons
        self.engine = engine
        self.channel = channel
        self.user = user
        self.message = None # This will be set by the engine after sending the message

        # Dynamically create a button for each action proposed by the AI
        for action_text in actions:
            # Truncate button label to Discord's 80 character limit
            label = action_text if len(action_text) <= 80 else action_text[:77] + "..."
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, custom_id=f"rpg_action_{action_text[:30]}")
            
            # Use a partial to "freeze" the action_text for the callback
            button.callback = functools.partial(self.action_callback, action=action_text)
            self.add_item(button)

    async def action_callback(self, interaction: discord.Interaction, action: str):
        # Acknowledge the interaction and disable the buttons to prevent double-clicks
        # Important: Defer the response first
        await interaction.response.defer()

        # Disable buttons on the original message
        for item in self.children:
            item.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except discord.NotFound:
            # The original message might have been deleted, which is fine.
            pass
        
        # We stop the view to prevent on_timeout from firing and trying to edit again
        self.stop()

        # The user's choice is posted as a new message for clarity in the chat log
        await self.channel.send(f"**{interaction.user.display_name}** chose to: *{action}*")

        # Re-construct the prompt as if the user typed it and send it to the engine
        prompt = f"{interaction.user.name}: {action}"
        await self.engine.process_turn(
            channel=self.channel,
            prompt=prompt,
            user=interaction.user,
            message_id=None # Pass None since this is from a button click
        )

    async def on_timeout(self):
        # When the view times out, disable all buttons
        for item in self.children:
            item.disabled = True
        # Try to edit the original message to show the view is inactive
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.NotFound:
                pass # Message was deleted, nothing to do
