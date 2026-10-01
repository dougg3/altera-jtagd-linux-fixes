# Intel/Altera Quartus jtagd Linux bug fixes

This is a Python script that patches the jtagd daemon bundled with various versions of Quartus for Linux to work around a couple of really annoying bugs that interfere with JTAG programming using USB Blasters. These problems have been in jtagd since at least version 18.1, and still exist in version 25.1 as of this writing.

Basically, these bugs cause long 30-second delays in JTAG operations, and one of them results in false `Unable to read device chain - JTAG chain broken` errors. If you see this error frequently even though you have a good setup, it's likely that the JTAG chain isn't broken. jtagd is what's broken.

## AI Disclosure

I [originally observed both of these bugs with my own (human) eyes](https://www.downtowndougbrown.com/2024/07/fixing-more-cheap-altera-usb-blaster-clones-cpld-adventures/), but I wasn't sure about the root cause of either of them. I handed a description of both problems to Claude Opus 5.5, which identified both issues and proposed these fixes. It also wrote the `fix_jtagd.py` script, which I then tweaked by hand to my liking. I then confirmed that the patch does indeed fix both problems and the explanation makes sense. These instructions are human-written.

## The bugs

There are two bugs in jtagd that this script patches:

1. Occasionally, if you have been unplugging and replugging your USB Blaster without restarting jtagd, it doesn't work correctly. You see a 30-second delay followed by a "JTAG chain broken" error no matter how many times you try, until you restart jtagd (or unplug and replug the blaster again). If you sniff the USB traffic, you will find that there is corruption in the outgoing traffic. The bug is caused by an uninitialized member variable in the `JTAG_SERVER_USB_BLASTER` class. If the variable happens to be greater than zero, jtagd sends traffic that is only valid for a USB Blaster II, even if your Blaster isn't compatible with the version 2 protocol.

2. The first time you attempt to access some FT245-based USB Blasters, jtagd sits around doing nothing for 30 seconds before it recognizes that something is wrong, retries, and then it starts working correctly. This 30-second delay is caused by a particular sequence of initialization commands sent to the FT245, which results in it ignoring data for 250 ms. jtagd sends its first query during that time, so the reply is lost. This issue only affects some USB Blaster devices, and at least on a subset of those devices, only happens after a USB reset. In any case, rearranging the initialization sequence works around it, and the fix seems to be harmless to unaffected devices.

## How to patch your jtagd

### Install prerequisites

You need Python 3 and [pyelftools](https://github.com/eliben/pyelftools). For Debian/Ubuntu, you can just run this command:

```
sudo apt install python3-pyelftools
```

Or install it into a virtual environment:

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Note that if you use a venv, you will need to run the script as `.venv/bin/python fix_jtagd.py` instead of `./fix_jtagd.py`.

### Stop jtagd

You will want to make sure jtagd isn't running while you patch it, so that the fix will actually go into effect. Run this command to stop jtagd:

```
killall jtagd
```

### Patch jtagd

First, locate your jtagd binary. With the default installation path that Quartus uses, it'll likely be in a folder such as `~/intelFPGA` or `~/altera`. Here are a couple of example paths for installations on my machine:

- `~/intelFPGA/18.1/qprogrammer/linux64/jtagd`
- `~/altera/25.1std/qprogrammer/linux64/jtagd`

Make sure you are looking in the `linux64` directory. `jtagd` in the `bin` directory is a wrapper shell script rather than the actual binary that needs patching.

Next, check to see if the patcher recognizes the binary as something that it can patch. Using the path in the first example above:

```
./fix_jtagd.py check ~/intelFPGA/18.1/qprogrammer/linux64/jtagd
```

You should see output like this:

```
/home/yourusername/intelFPGA/18.1/qprogrammer/linux64/jtagd:
  uninitialized variable: needs patching
  30 second delay: needs patching
```

Assuming it recognizes that the bugs need to be patched, you can run the following command to patch it:

```
./fix_jtagd.py apply ~/intelFPGA/18.1/qprogrammer/linux64/jtagd
```

Here is what successful output looks like:

```
/home/yourusername/intelFPGA/18.1/qprogrammer/linux64/jtagd:
  uninitialized variable: needs patching
  30 second delay: needs patching
  saved original to /home/yourusername/intelFPGA/18.1/qprogrammer/linux64/jtagd.orig
  patched 5 locations, wrote /home/yourusername/intelFPGA/18.1/qprogrammer/linux64/jtagd
```

It saves the original binary to `jtagd.orig` in case you want to restore it.

**Note:** If you have full Quartus rather than the standalone programmer, your binary will be in `quartus/linux64/jtagd` instead. If you have both the full Quartus and the standalone programmer installed, make sure to patch both.

## Known affected versions

The following versions of Quartus have all been observed to be affected by these bugs. It's likely that all versions between 18.1 and 25.1 are affected. These are just the ones I've tested.

- 18.1
- 20.1
- 21.1
- 23.1
- 24.1
- 25.1

## Known affected USB Blasters and clones

Without this patch, I have observed the first bug on multiple USB Blaster clones, including the officially-supported Terasic USB Blaster.

The second delay bug is less common. I've been able to reproduce it after every hotplug on a generic FT245-based USB Blaster obtained from AliExpress. My Terasic blaster is also affected by this problem at times, but only after a USB reset. It's unclear to me if it only affects certain FT245 chips or what the common denominator is, but this fix makes the problem completely go away.

## License

This project is licensed under the MIT license. See the [LICENSE](LICENSE) file for more details.
