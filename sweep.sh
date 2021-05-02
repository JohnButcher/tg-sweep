#!/bin/bash
#
# Run this as cron from a user who has sudo access
# - suggest copying this script to a directory called $HOME/sweep
#
# */7 * * * * /home/pi/sweep/sweep.sh >>/tmp/sweep-cron.log 2>&1
#
set -e
here=$(readlink -f $(dirname $BASH_SOURCE))
owner=$(stat --print='%U:%G' $BASH_SOURCE)
logdir=$here/log
mkdir -p $logdir
logfile=$logdir/sweep-$(date +"%A").log
find $logdir -name '*.log' -mtime +5 -exec rm {} \;
if [ $(id -un) != "root" ];then
   exec sudo /bin/bash $BASH_SOURCE "$@"
fi
if [ ! -s $here/sweep.json ];then
   echo "ERROR - where is sweep.json?"
   exit 1
fi
if [ ! -d $here/ve ];then
   python3 -m venv $here/ve
   source $here/ve/bin/activate
   pip install pip --upgrade
   deactivate
fi
needs_refresh=$(find $here -name 'git' -type d -ctime +0.5)
if [ -n "$needs_refresh" ] || [ ! -d $here/git ];then
   rm -rf $here/git
   mkdir $here/git
   cd $here/git
   git clone https://github.com/JohnButcher/tg-sweep
   cp $here/sweep.json $here/git/tg-sweep/
fi
cd $here/git/tg-sweep
source $here/ve/bin/activate
pip install -r $here/git/tg-sweep/requirements.txt --upgrade
chown -R $owner $here/*
python sweep.py >>$logfile 2>&1
cat $logfile
#
# End
