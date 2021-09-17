import asyncio
import functools
import itertools
import math
import random
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import discord
from discord.ext import commands,tasks
import youtube_dl
from async_timeout import timeout
from random import choices
from random import choice
from discord.utils import get
from discord.ext.commands import has_permissions, MissingPermissions


cid = 'your cid' #spotify api
secret = 'your secret'
youtube_dl.utils.bug_reports_message = lambda: ''
client_credentials_manager = SpotifyClientCredentials(client_id=cid, client_secret=secret)
sp = spotipy.Spotify(client_credentials_manager=client_credentials_manager)

class VoiceError(Exception):
    pass


class YTDLError(Exception):
    pass


class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
    }

    FFMPEG_OPTIONS = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn',
    }

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    def __init__(self, ctx: commands.Context, source: discord.FFmpegPCMAudio, *, data: dict, volume: float = 0.5):
        super().__init__(source, volume)

        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data

        self.uploader = data.get('uploader')
        self.uploader_url = data.get('uploader_url')
        date = data.get('upload_date')
        self.upload_date = date[6:8] + '.' + date[4:6] + '.' + date[0:4]
        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = self.parse_duration(int(data.get('duration')))
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.views = data.get('view_count')
        self.likes = data.get('like_count')
        self.dislikes = data.get('dislike_count')
        self.stream_url = data.get('url')

    def __str__(self):
        return '**{0.title}** by **{0.uploader}**'.format(self)

    @classmethod
    async def create_source(cls, ctx: commands.Context, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()

        partial = functools.partial(cls.ytdl.extract_info, search, download=False, process=False)
        data = await loop.run_in_executor(None, partial)

        if data is None:
            raise YTDLError('Couldn\'t find anything that matches `{}`'.format(search))

        if 'entries' not in data:
            process_info = data
        else:
            process_info = None
            for entry in data['entries']:
                if entry:
                    process_info = entry
                    break

            if process_info is None:
                raise YTDLError('Couldn\'t find anything that matches `{}`'.format(search))

        webpage_url = process_info['webpage_url']
        partial = functools.partial(cls.ytdl.extract_info, webpage_url, download=False)
        processed_info = await loop.run_in_executor(None, partial)

        if processed_info is None:
            pass
            #raise YTDLError('Couldn\'t fetch `{}`'.format(webpage_url))

        if 'entries' not in processed_info:
            info = processed_info
        else:
            info = None
            while info is None:
                try:
                    info = processed_info['entries'].pop(0)
                except IndexError:
                    raise YTDLError('Couldn\'t retrieve any matches for `{}`'.format(webpage_url))

        return cls(ctx, discord.FFmpegPCMAudio(info['url'], **cls.FFMPEG_OPTIONS), data=info)

    @staticmethod
    def parse_duration(duration: int):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        duration = []
        if days > 0:
            duration.append('{} days'.format(days))
        if hours > 0:
            duration.append('{} hours'.format(hours))
        if minutes > 0:
            duration.append('{} minutes'.format(minutes))
        if seconds > 0:
            duration.append('{} seconds'.format(seconds))

        return ', '.join(duration)


class Song:
    __slots__ = ('source', 'requester')

    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        embed = (discord.Embed(title='Now playing',
                               description='```css\n{0.source.title}\n```'.format(self),
                               color=discord.Color.blurple())
                 .add_field(name='Duration', value=self.source.duration)
                 .add_field(name='Requested by', value=self.requester.mention)
                 .add_field(name='Uploader', value='[{0.source.uploader}]({0.source.uploader_url})'.format(self))
                 .add_field(name='URL', value='[Click]({0.source.url})'.format(self))
                 .set_thumbnail(url=self.source.thumbnail))

        return embed


class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]


class VoiceState:
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self._ctx = ctx

        self.current = None
        self.voice = None
        self.next = asyncio.Event()
        self.songs = SongQueue()

        self._loop = False
        self._volume = 0.5
        self.skip_votes = set()

        self.audio_player = bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = value

    @property
    def is_playing(self):
        return self.voice and self.current

    async def audio_player_task(self):
        while True:
            self.next.clear()
            self.now = None

            if self.loop == False:
                # Try to get the next song within 3 minutes.
                # If no song will be added to the queue in time,
                # the player will disconnect due to performance
                # reasons.
                try:
                    async with timeout(180):  # 3 minutes
                        self.current = await self.songs.get()
                except asyncio.TimeoutError:
                    self.bot.loop.create_task(self.stop())
                    self.exists = False
                    return

                self.current.source.volume = self._volume
                self.voice.play(self.current.source, after=self.play_next_song)
                await self.current.source.channel.send(embed=self.current.create_embed())

            # If the song is looped
            elif self.loop == True:
                self.now = discord.FFmpegPCMAudio(self.current.source.stream_url, **YTDLSource.FFMPEG_OPTIONS)
                self.voice.play(self.now, after=self.play_next_song)

            await self.next.wait()

    def play_next_song(self, error=None):
        if error:
            raise VoiceError(str(error))

        self.next.set()

    def skip(self):
        self.skip_votes.clear()

        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect()
            self.voice = None


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_states = {}

    def get_voice_state(self, ctx: commands.Context):
        state = self.voice_states.get(ctx.guild.id)
        if not state:
            state = VoiceState(self.bot, ctx)
            self.voice_states[ctx.guild.id] = state

        return state

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    def cog_check(self, ctx: commands.Context):
        if not ctx.guild:
            raise commands.NoPrivateMessage('This command can\'t be used in DM channels.')

        return True

    async def cog_before_invoke(self, ctx: commands.Context):
        ctx.voice_state = self.get_voice_state(ctx)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        await ctx.send('An error occurred: {}'.format(str(error)))

    @commands.command(name='join', invoke_without_subcommand=True)
    async def _join(self, ctx: commands.Context):
        """Joins a voice channel."""

        destination = ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name='summon')
    async def _summon(self, ctx: commands.Context, *, channel: discord.VoiceChannel = None):
        """Summons the bot to a voice channel.
        If no channel was specified, it joins your channel.
        """

        if not channel and not ctx.author.voice:
            raise VoiceError('You are neither connected to a voice channel nor specified a channel to join.')

        destination = channel or ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name='leave', aliases=['disconnect','dc','fuckoff'])
    async def _leave(self, ctx: commands.Context):
        """Clears the queue and leaves the voice channel."""

        if not ctx.voice_state.voice:
            return await ctx.send('Not connected to any voice channel.')

        await ctx.voice_state.stop()
        await ctx.message.add_reaction('👋')
        del self.voice_states[ctx.guild.id]

    @commands.command(name='volume')
    async def _volume(self, ctx: commands.Context, *, volume: int):
        """Sets the volume of the player."""

        if not ctx.voice_state.is_playing:
            return await ctx.send('Nothing being played at the moment.')

        elif ((volume in range(201))==False):
            return await ctx.send('Volume must be between 0 and 200')
        else:
            ctx.voice_state.current.source.volume = volume / 100
            await ctx.send('Volume of the player set to {}%'.format(volume))

    @commands.command(name='now', aliases=['current', 'playing'])
    async def _now(self, ctx: commands.Context):
        """Displays the currently playing song."""
        if (len(ctx.voice_state.songs) == 0) and (ctx.voice_state.voice.is_playing()==False):
            await  ctx.send('No song is currently being played.')
        else:
            await ctx.send(embed=ctx.voice_state.current.create_embed())

    @commands.command(name='pause')
    async def _pause(self, ctx: commands.Context):
        """Pauses the currently playing song."""

        if ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.pause()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='resume',aliases=['r','unpause'])
    async def _resume(self, ctx: commands.Context):
        """Resumes a currently paused song."""

        if ctx.voice_state.voice.is_paused():
            ctx.voice_state.voice.resume()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='clear')
    async def _clear(self, ctx: commands.Context):
        ctx.voice_state.songs.clear()
        await ctx.message.add_reaction('🗑️')

    """@commands.command(name='stop')
    async def _stop(self, ctx: commands.Context):
       Stops playing song and clears the queue.

        ctx.voice_state.songs.clear()

        if ctx.voice_state.is_playing:
            ctx.voice_state.voice.stop()
            await ctx.message.add_reaction('⏹')"""

    @commands.command(name='skip',aliases=['s'])
    async def _skip(self, ctx: commands.Context):
        """Vote to skip a song. The requester can automatically skip.
        3 skip votes are needed for the song to be skipped.
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send('Not playing any music right now...')

        voter = ctx.message.author
        if voter == ctx.voice_state.current.requester:
            await ctx.message.add_reaction('⏭')
            ctx.voice_state.skip()

        elif voter.id not in ctx.voice_state.skip_votes:
            ctx.voice_state.skip_votes.add(voter.id)
            total_votes = len(ctx.voice_state.skip_votes)

            if total_votes >= 1:
                await ctx.message.add_reaction('⏭')
                ctx.voice_state.skip()
            else:
                await ctx.send('Skip vote added, currently at **{}/3**'.format(total_votes))

        else:
            await ctx.send('You have already voted to skip this song.')

    @commands.command(name='queue', aliases=['q'])
    async def _queue(self, ctx: commands.Context, *, page: int = 1):
        """Shows the player's queue.
        You can optionally specify the page to show. Each page contains 10 elements.
        """

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        items_per_page = 10
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue = ''
        for i, song in enumerate(ctx.voice_state.songs[start:end], start=start):
            queue += '`{0}.` [**{1.source.title}**]({1.source.url})\n'.format(i + 1, song)

        embed = (discord.Embed(description='**{} tracks:**\n\n{}'.format(len(ctx.voice_state.songs), queue))
                 .set_footer(text='Viewing page {}/{}'.format(page, pages)))
        await ctx.send(embed=embed)

    @commands.command(name='shuffle')
    async def _shuffle(self, ctx: commands.Context):
        """Shuffles the queue."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        ctx.voice_state.songs.shuffle()
        await ctx.message.add_reaction('✅')

    @commands.command(name='remove')
    async def _remove(self, ctx: commands.Context, index: int):
        """Removes a song from the queue at a given index."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        ctx.voice_state.songs.remove(index - 1)
        await ctx.message.add_reaction('✅')

    @commands.command(name='loop')
    async def _loop(self, ctx: commands.Context):
        """Loops the currently playing song.
        Invoke this command again to unloop the song.
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send('Nothing being played at the moment.')

        # Inverse boolean value to loop and unloop.
        ctx.voice_state.loop = not (ctx.voice_state.loop)
        await ctx.message.add_reaction('✅')

    @commands.command(name='p',aliases=['play','baja'])
    async def _play(self, ctx: commands.Context, *, search: str):
        """Plays a song.
        If there are songs in the queue, this will be queued until the
        other songs finished playing.
        This command automatically searches from various sites if no URL is provided.
        A list of these sites can be found here: https://rg3.github.io/youtube-dl/supportedsites.html
        """

        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)
        if 'spotify' in search and 'playlist' in search:
            playlistt = sp.playlist(str(search))
            pl_id = playlistt['uri']
            results = sp.playlist(pl_id)
            tracks = []

            for item in results['tracks']['items']:
                tracks.append(
                    item['track']['name'] + ' ' +
                    item['track']['artists'][0]['name'] + ' ' +
                    item['track']['album']['name']
                )

            for s in tracks:
                try:
                    source = await YTDLSource.create_source(ctx, s, loop=self.bot.loop)
                except YTDLError as e:
                    await ctx.send('An error occurred while processing this request: {}'.format(str(e)))
                    await ctx.send('Make sure that the playlist is **public**')
                else:
                    song = Song(source)

                    await ctx.voice_state.songs.put(song)

        else:
            async with ctx.typing():
                try:
                    source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
                except YTDLError as e:
                    await ctx.send('An error occurred while processing this request') #: {}'.format(str(e))
                    await ctx.send('Try Again')
                else:
                    song = Song(source)

                    await ctx.voice_state.songs.put(song)
                    await ctx.send('Enqueued {}'.format(str(source)), delete_after=30)

    @_join.before_invoke
    @_play.before_invoke
    async def ensure_voice_state(self, ctx: commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise commands.CommandError('You are not connected to any voice channel.', delete_after=15)

        if ctx.voice_client:
            if ctx.voice_client.channel != ctx.author.voice.channel:
                raise commands.CommandError('Bot is already in a voice channel.')


intents = discord.Intents().all()


client = commands.Bot(command_prefix='.',intents=intents)

client.add_cog(Music(client))
status = ['Submitting test papers!', 'Minecrafting', 'Gamering', 'Baking Cookies']

@client.event
async def on_ready():
    change_status.start()
    print('Bot is online!')


@client.event
async def on_member_join(member):
    channel = discord.utils.get(member.guild.channels, name='general')
    await channel.send(f'Welcome {member.mention}!  Ready to jam out? See `.help` command for details!')


@client.command(name='ping',help='This command returns the latency obv ')
async def ping(ctx):
    await ctx.send(f'**Pong!** Latency:{round(client.latency*1000)}ms')





@client.command(name='hlo', help='This command returns a random welcome message (sfw)', aliases=['hello','hi'])
async def hello(ctx):
    if ctx.message.guild.id == 883222034069475379:
        responses=[f'Hello {ctx.author.mention}', 'Hi :blush:', ':cookie:','Stop talking to a bot! Padhai karlo :nerd:']
        var = choices(responses, k=3)
        await ctx.send(choice(var))


@client.command(name='credits', help='This command returns the credits')
async def credits(ctx):
    await ctx.send('Made by Thara Bhai Bhuv')


@client.command(name='8ball', help='Generic 8ball game')
async def ball8(ctx):
    if ctx.message.guild.id != 883222034069475379:
        response=['Obviously', 'No!', 'K bro', 'Go ask your mum!', 'Sure ig']
        var=choices(response,k=3)
        await ctx.send(choice(var))
    else:
        await ctx.send("Mr.Tanmay doesn't want 8ball")





keyword='LMFAO'
repl=['D','S','K','F']

@client.event
async def on_message(message):
    if message.guild.id == 883222034069475379:
        message_text = message.content.strip().upper()
        for x in message.mentions:
            if(x==client.user):
                await message.channel.send(f"Ping :eyes:")
    else:
        message_text = message.content.strip().upper()
        for x in message.mentions:
            if (x == client.user):
                await message.channel.send(f"Ping mat kar oyy :rage:")



    if client.user.id != message.author.id:
        if message.guild.id != 883222034069475379:
            if keyword in message_text:
                for i in range(3):
                    rewl = choice(repl)
                    temp = keyword.replace(keyword[random.randint(0, 4)], rewl)
                    await message.channel.send(temp + ('O' * random.randint(0, 5)))
            elif ':pepekek:' in message_text.lower():
                await message.channel.send('<:pepekek:866676292383146014>')
    await client.process_commands(message)


@client.command(name='purge', pass_context=True, help='Mass delete messages')
async def clean(ctx, limit: int):
    if (ctx.author.id ==457928171527077889 or ctx.message.author.guild_permissions.manage_messages) and limit<=150:
        await ctx.channel.purge(limit=limit+1)
        await ctx.send('Cleared by {}'.format(ctx.author.mention),delete_after=0.5)
        await ctx.message.delete()
    else:
        await ctx.send("You cant do that!")


@client.command(name='ghostping', help='Ghostping people')
async def ghostping(ctx, user:discord.Member,limit: int):
    if limit <=50 and limit>0 and user.id!=457928171527077889 and user.id!=886857314223681536 :  #and ctx.message.guild.id != 883222034069475379
        await ctx.channel.purge(limit=1)
        for i in range(limit):
            await ctx.send(user.mention,delete_after=0.00001)
    else:
        await ctx.send('Invalid')



@client.command(name='spamping', help='Spam pinging people people')
async def spamping(ctx, user:discord.Member,limit: int):
    if limit <=50 and limit>0 and user.id!=457928171527077889 and user.id!=886857314223681536:
        for i in range(limit):
            await ctx.send(user.mention)
    else:
        await ctx.send('Invalid')



@client.command(name='samtunakk', pass_context=True, hidden=True)
async def samtunakk(ctx):
    if ctx.author.id ==457928171527077889:
        await ctx.channel.send('https://cdn.discordapp.com/attachments/770232624932192277/887562971998461992/video_1631681810640.mp4')


@client.command(name='mute',pass_context=True, help='Mute a user in a VC')
async def mute(ctx, user:discord.Member):
    if ctx.author.id==457928171527077889 or ctx.message.author.guild_permissions.administrator:
        await user.edit(mute=True)


@client.command(name='unmute',pass_context=True, help='Unmute a user in a VC')
async def unmute(ctx, user:discord.Member):
    if ctx.author.id==457928171527077889 or ctx.message.author.guild_permissions.administrator:
        await user.edit(mute=False)


@client.command(name='deafen',pass_context=True, help='Deafen a user in a VC')
async def deafen(ctx, user:discord.Member):
    if ctx.author.id==457928171527077889 or ctx.message.author.guild_permissions.administrator:
        await user.edit(deafen=True)


@client.command(name='undeafen',pass_context=True, help='Undeafen a user in a VC')
async def undeafen(ctx, user:discord.Member):
    if ctx.author.id==457928171527077889 or ctx.message.author.guild_permissions.administrator:
        await user.edit(deafen=False)


@client.command(name='bye',pass_context = True, help='Disconnect a user from a VC')
async def bye(ctx: commands.Context, user: discord.Member):
    if ctx.author.id == 457928171527077889 or ctx.message.author.guild_permissions.administrator:
        await ctx.send(f'{user.mention} has been kicked from {user.voice.channel.mention}',delete_after=1)
        await user.move_to(None)



@client.command(name='cookie',pass_context=True,help='Cookies')
async def cookie(ctx):
    await ctx.send('Here have a cookie :cookie: :)')



@tasks.loop(seconds=20)
async def change_status():
    await client.change_presence(activity=discord.Game(choice(status)))


client.run('yourtoken')