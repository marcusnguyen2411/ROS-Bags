# ROS bag transfer

Copy a completed ROS 2 bag directory from Karbon to a laptop over SSH. GitHub
hosts the transfer script; the recorded bag goes directly from Karbon to the
laptop and is not uploaded to GitHub.

Run this tool in Ubuntu on the laptop, including Ubuntu in WSL2. It preserves
an interrupted download so the same command can resume it, and it never replaces
an existing completed bag directory. It copies the files without changing or
deleting the bag on Karbon. ROS 2 is not required to copy the files.

## One-time setup

1. Connect the laptop and Karbon using a network that permits SSH. For remote
   access, a private network such as [Tailscale](https://tailscale.com/docs/how-to/connect-to-devices)
   is one option. Both computers must be reachable and allowed by its policies.
   GitHub does not provide the network connection between the computers.
2. Install Python 3, `rsync`, and an SSH client in laptop Ubuntu:

   ```bash
   sudo apt update
   sudo apt install python3 rsync openssh-client
   ```

3. Karbon needs `rsync` and a working SSH service, and your Karbon user needs
   permission to read the bag. Test the connection **from laptop Ubuntu**:

   ```bash
   ssh KARBON_USER@KARBON_HOST
   ```

   Replace `KARBON_USER` with the Karbon Linux username, and `KARBON_HOST`
   with its private IP address, DNS name, or an SSH config alias. You may be
   prompted for SSH authentication and host-key confirmation. If this command
   fails, fix the network or SSH setup before using the downloader. For custom
   ports or keys, configure a host alias in `~/.ssh/config` on the laptop.

4. Clone the repository after it has been published, or copy `fetch_bag.py`
   to the laptop. Open a terminal in the directory containing the script.

## Transfer a bag

1. On Karbon, stop recording and wait for **Saving** to disappear in the
   Vehicle UI. By default the UI saves bags in the `recordings/` directory of
   the 1811 checkout; the UI can also be configured to save elsewhere. Find
   the absolute path of the bag directory on the **Karbon host**, for example:

   ```bash
   cd /path/to/1811
   pwd
   ls -1 recordings
   ```

   Use the directory name listed under `recordings`, not a single `.db3` or
   `.mcap` file inside it. A container path such as `/vehicle_1811/recordings`
   is not necessarily the same as its path on the Karbon host.

2. From laptop Ubuntu, run this command, replacing every placeholder:

   ```bash
   python3 fetch_bag.py 'KARBON_USER@KARBON_HOST:/absolute/path/to/1811/recordings/BAG_NAME'
   ```

   The completed bag appears at `~/rosbags/BAG_NAME`. To save it elsewhere:

   ```bash
   python3 fetch_bag.py 'KARBON_USER@KARBON_HOST:/absolute/path/to/1811/recordings/BAG_NAME' \
     --destination '/path/to/local/folder'
   ```

   Keep the quotes if a path contains spaces. The SSH host may be a hostname,
   IPv4 address, or SSH config alias; use an alias for an IPv6 literal. To
   browse the default destination in Windows Explorer, run:

   ```bash
   cd ~/rosbags
   explorer.exe .
   ```

If the connection drops or you press Ctrl+C, repeat the **same command**.
Partial files are stored under a hidden `.BAG_NAME.partial` directory within
the destination; do not remove it if you want to resume. A bag with the same
name that has already finished downloading is never overwritten. Select
another destination if needed.

Only copy a bag once recording and saving have finished. The script checks for
`metadata.yaml`, but its presence does not prove that every recording is
playable. To inspect the downloaded bag on a system with ROS 2 installed:

```bash
ros2 bag info ~/rosbags/BAG_NAME
```

The no-overwrite publication uses Linux `renameat2`; if the destination
filesystem does not support it, the completed download remains in the partial
directory. Choose a supported Linux filesystem such as the WSL home directory.

## Run the checks

```bash
python3 -m unittest discover -s tests -v
```

The integration checks use local temporary files and a simulated SSH transport;
they do not connect to Karbon.
