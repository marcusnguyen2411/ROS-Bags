# ROS bags and file transfers

Upload completed ROS 2 recordings from Karbon to GitHub automatically, upload
older bags or other Linux files manually, and download them on a laptop. The
original files remain on the computer where they were recorded.

**This repository is public. Every published recording and file can be
downloaded by anyone**, including any camera images, location data, or other
information inside. Upload only data you intend to make public.

Uploads appear under [Releases](https://github.com/marcusnguyen2411/ROS-Bags/releases),
not in the repository's source-code file list. GitHub blocks ordinary Git
files larger than 100 MiB, so the uploader packages each file or directory into
512 MiB release assets. It publishes the release only after all parts are
uploaded and verified. See [GitHub's size limits](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)
and [release asset limits](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases).
The automatically generated “Source code” downloads in a GitHub release contain
the uploader code, not the bag; use the commands below or the `upload.tar.part...`
assets to retrieve the actual upload.

## Set up automatic uploads on Karbon

You need Git, Python 3, GitHub CLI, internet access, and a systemd user session
on **Karbon**. The laptop does not need to be online while Karbon uploads.
Run the following on the Karbon host, outside the Docker container:

```bash
sudo apt update
sudo apt install git python3 gh
gh auth login --hostname github.com --web
git clone https://github.com/marcusnguyen2411/ROS-Bags.git ~/ROS-Bags
cd ~/ROS-Bags
```

Sign in to the GitHub account that has write access to `ROS-Bags` when asked.
If `~/ROS-Bags` already exists, update that checkout instead of cloning over it.
The 1811 documentation lists its Karbon checkout as `~/1811-fall-2026`. If
yours is elsewhere, substitute its actual path in the next command:

```bash
python3 github_upload.py setup-auto --checkout "$HOME/1811-fall-2026"
```

Setup checks GitHub access, applies the bundled recorder integration to that
checkout only if it can do so without conflicts, and enables a background user
service. The recorder integration appears as local changes in the 1811 checkout;
review them with `git -C "$HOME/1811-fall-2026" diff`. If the patch cannot apply
cleanly, setup stops instead of overwriting conflicting changes. Restart the
stack with `./g` from the 1811 checkout so it rebuilds the hub.

Once set up, the recorder queues a bag only after its process exits successfully
and `metadata.yaml` exists. Keep **Save to** inside the 1811 checkout, such as
its default `recordings/` directory. A bag saved elsewhere remains local; the
hub reports that it was not queued, and you can upload it manually instead.
The message **Queued** means the bag is waiting for the background service; it
does not mean the upload is complete.

The service retries queued uploads after network or authentication failures and
reuses parts GitHub has already received. It runs while the Karbon user session
and service are running; after a reboot it starts again when that user session
starts. Do not edit a bag while it is being uploaded. The uploader needs up to
one additional 512 MiB temporary part on Karbon, and large recordings can use
substantial disk bandwidth and internet data.

Check pending jobs, errors, and finished release links on Karbon:

```bash
cd ~/ROS-Bags
python3 github_upload.py status --checkout "$HOME/1811-fall-2026"
```

Setup also prints a `journalctl --user -u ... -f` command for the upload log.
If login expires, run `gh auth login` again on Karbon. Queue information lives
in the checkout's ignored `.rosbag-upload/` folder, with no token stored there.

## Upload an older bag or another Linux file

Run on the computer that holds the file, after the GitHub login described
above. A ROS bag is a directory containing `metadata.yaml` and its `.db3` or
`.mcap` files; pass the whole directory:

```bash
cd ~/ROS-Bags
python3 github_upload.py upload '/absolute/path/to/old_bag_directory'
python3 github_upload.py upload '/absolute/path/to/another_file'
```

Each command creates its own public release. It prints a job ID. If interrupted,
repeat using that ID; completed parts are reused:

```bash
python3 github_upload.py retry JOB_ID
python3 github_upload.py status
```

Files and folders must not contain symbolic links or special files. Use a copy
without them if needed. Do not change a file or folder during upload.

## Download on your laptop

In Ubuntu or WSL on the laptop, clone this public repository and list uploads:

```bash
git clone https://github.com/marcusnguyen2411/ROS-Bags.git
cd ROS-Bags
python3 github_upload.py list
```

Copy the `data-...` tag printed for the upload you want and run:

```bash
python3 github_upload.py download data-REPLACE_WITH_TAG
```

The script verifies the manifest and every part, then reconstructs the original
file or directory under `~/rosbags/`. It refuses to overwrite an existing file
or directory. Use `--destination '/path/to/folder'` to choose another location.
Downloaded parts are retained if the connection drops; rerun the same command.
If interrupted during extraction, move the `extracted` directory mentioned in
the error out of the hidden partial directory before retrying. During extraction
the laptop needs space for both the downloaded parts and the extracted data.
On WSL, open the default destination in Windows Explorer with:

```bash
cd ~/rosbags
explorer.exe .
```

Check a downloaded recording on a system with ROS 2 installed using
`ros2 bag info ~/rosbags/BAG_NAME`. GitHub can also be used in a browser:
download every `upload.tar.part...` asset from the release into an empty folder,
then run `cat upload.tar.part* | tar -xf -` in that folder.

## Alternative: transfer directly from Karbon over SSH

For a private direct transfer without uploading the data to GitHub, install
Python 3, `rsync`, and an SSH client in laptop Ubuntu; Karbon also needs `rsync`
and an SSH service. Both machines need network connectivity, for example through
[Tailscale](https://tailscale.com/docs/how-to/connect-to-devices). After the bag
has stopped saving, run on the laptop:

```bash
python3 fetch_bag.py 'KARBON_USER@KARBON_HOST:/absolute/path/to/1811/recordings/BAG_NAME'
```

The result goes to `~/rosbags/BAG_NAME`. Repeat the same command to resume an
interrupted transfer. For a different destination, add
`--destination '/path/to/local/folder'`. The script never replaces an existing
bag. Presence of `metadata.yaml` is a basic check, not proof the bag is playable.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
