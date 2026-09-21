import os
import subprocess
import time

def main():
    print("Starting rebase...")
    env = os.environ.copy()
    env['GIT_SEQUENCE_EDITOR'] = 'python seq_editor.py'
    env['GIT_COMMITTER_NAME'] = 'Shashank17singh'
    env['GIT_COMMITTER_EMAIL'] = 'shashanksingh1709@gmail.com'
    
    # Start rebase
    p = subprocess.Popen(['git', 'rebase', '-i', '--root'], env=env)
    p.wait()
    
    # Loop while rebase is in progress
    while True:
        res = subprocess.run(['git', 'status'], capture_output=True, text=True)
        if 'interactive rebase in progress' not in res.stdout.lower() and 'rebase in progress' not in res.stdout.lower():
            print("Rebase finished!")
            break
            
        print("Amending commit...")
        subprocess.run(['git', 'commit', '--amend', '--author=Shashank17singh <shashanksingh1709@gmail.com>', '--no-edit'], env=env)
        
        print("Continuing rebase...")
        subprocess.run(['git', 'rebase', '--continue'], env=env)
        
        time.sleep(0.5)

if __name__ == '__main__':
    main()
