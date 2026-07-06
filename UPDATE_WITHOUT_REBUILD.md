# How to Update AnTiMa Without Rebuilding Docker

Because your `docker-compose.yml` does not currently bind-mount the source code into the container (it copies the code into the image during the `docker build` phase), any changes to the code require either a full rebuild or a direct file injection. 

If you want to apply the recent RPG token optimization updates on your local Ubuntu server **without** running a time-consuming `docker compose build`, you can use one of the two methods below.

---

## Method 1: The `docker cp` Injection (Fastest for one-off patches)

This method directly copies the modified Python files from your host Ubuntu server into the running Docker container, and then restarts the bot to apply the changes.

### Step 1: Pull the latest code to your host
On your Ubuntu server, navigate to the bot's root directory and pull the latest changes:
```bash
cd /path/to/AnTiMa
git pull origin main  # Or whatever branch you are using
```

### Step 2: Copy the modified files into the container
Use the `docker cp` command to push the specific files we just updated directly into the `antima-bot` container's `/app` directory:
```bash
docker cp cogs/rpg_system/engine.py antima-bot:/app/cogs/rpg_system/engine.py
docker cp cogs/rpg_system/tools.py antima-bot:/app/cogs/rpg_system/tools.py
docker cp cogs/rpg_system/memory.py antima-bot:/app/cogs/rpg_system/memory.py
```

### Step 3: Restart the Bot
For the Python files to be re-evaluated, you must restart the bot container:
```bash
docker compose restart antima-bot
```
*(The bot will take a few seconds to boot back up, and the token optimization will be live!)*

---

## Method 2: The Volume Mount Method (Best for long-term development)

If you plan on making frequent updates and NEVER want to rebuild the image for simple code changes again, you should bind-mount your host directory directly into the container.

### Step 1: Edit your `docker-compose.yml`
Open `docker-compose.yml` on your server and modify the `volumes:` section under the `antima-bot` service to include `./:/app`:

```yaml
  antima-bot:
    image: antima-bot:latest
    build: .
    container_name: antima-bot
    restart: unless-stopped
    depends_on:
      - mongo
    ports:
      - "8000:8000"
    env_file:
      - .env
    environment:
      MONGO_URI: "mongodb://mongo:27017/antima_db"
      PORT: "8000"
    volumes:
      - antima-logs:/app/logs
      - ./:/app          # <--- ADD THIS LINE
```

### Step 2: Apply the Compose Change
Run the following command. It will **not** rebuild the Docker image, but it will quickly recreate the container using the new volume mapping:
```bash
docker compose up -d
```

### Future Updates
Once Method 2 is applied, any time you run `git pull` on your Ubuntu server, the changes instantly appear inside the Docker container. You will only ever need to run:
```bash
docker compose restart antima-bot
```
...and the bot will load the fresh code. No rebuilds required!

---

> [!TIP]
> **When WILL you need to rebuild?**
> You only need to run `docker compose build` if you change your `requirements.txt` (installing new pip packages) or if you modify the `Dockerfile` itself. Python code edits never technically require a rebuild if you use Method 1 or 2!
