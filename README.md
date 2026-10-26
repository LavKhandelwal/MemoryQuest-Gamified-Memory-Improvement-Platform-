**Memory Quest**


Memory Quest is a memory-training platform built with Python, Gradio, SQLite, and Matplotlib. It provides interactive memory games, adaptive difficulty, daily challenges, spaced repetition, progress tracking, and gamification to help users practice and monitor their recall performance.


Features:

1. User Accounts

• User registration and login.

• Passwords protected using PBKDF2 hashing.

• User profiles with age group and learning goal.

• Persistent user progress stored in an SQLite database.



2. Unscramble Memory Game

• Displays a sequence of letters and digits for a limited time.

• Hides and shuffles the sequence before recall.

• Requires users to reconstruct the original sequence.

• Supports practice, baseline testing, and re-testing.



3. Baseline Test and Re-test

•	Provides five fixed rounds for each test.

•	Establishes a baseline memory performance level.

•	Compares re-test results with baseline performance to track improvement.



4. Adaptive Difficulty

•	Unscramble practice adjusts difficulty according to rolling accuracy:

•	Accuracy above 95% increases difficulty.

•	Accuracy below 75% decreases difficulty.

•	The thresholds are designed around an 85% target accuracy.

•	Word Chain Auto mode increases difficulty after two consecutive wins and decreases it after two consecutive losses.



5. Word Chain Game

•	Players begin with a word, and the computer adds another word.

•	Players must recall the entire chain in the correct order and add a new word.

•	The first player to make a mistake loses.

•	Easy, Medium, and Hard modes control computer memory errors and word length.

•	Hints are limited to maintain the challenge.



6. Daily Quests

•	Provides daily memory-training tasks.

•	Awards experience points (XP) once per quest per day.

•	Offers a completion bonus for finishing all daily quests.

•	Includes a recall challenge in which users reproduce a previously hidden list.



7. Spaced Repetition

•	Available through the Review tab.

•	Brings back sequences and words that users previously missed.

•	Each successful recall doubles an item's half-life.

•	Each failed recall halves its half-life.

•	An item becomes due for review when its predicted recall probability falls below 70%.

The review scheduler uses a simplified half-life approach inspired by reference [6]. It is not a fitted or scientifically calibrated prediction model.



8. Gamification

•	Experience points (XP).

•	Player levels.

•	Daily activity streaks.

•	Ten achievement badges.



9. Progress Dashboard

•	Accuracy and level chart.

•	Fourteen-day activity chart.

•	Progress summary table.

•	CSV export for further analysis.





Technology Stack [Technology: Purpose]: 

Python: Core application logic

Gradio: Interactive web interface

SQLite: Persistent local database

Matplotlib: Data visualization

CSV: Progress data export





Project Structure:

The main application is implemented in a single Python file.

Memory-Quest/

├── memory_quest.py

├── requirements.txt

├── memory_quest_colab.ipynb

├── memory_quest.db          # Created when the app runs

└── README.md


Note: The database and notebook are included in the structure only if they exist in your project directory. The SQLite database is created when the application initializes it.

The older file memoryquest.py (without the underscore), if present in the outputs folder, is a separate legacy Flask file and is not part of this Gradio build.




Installation and Usage:

Option 1: Run in Visual Studio Code

Prerequisites

•	Python installed on your system.

•	Visual Studio Code with a Python environment configured.

•	The project files downloaded to your computer.

Steps

1.	Open the project folder in VS Code.

2.	Open the integrated terminal.

3.	Install the required dependencies:

pip install -r requirements.txt

4.	Start the application:

python memory_quest.py

5.	Open the following address in your browser:

http://127.0.0.1:7860

6.	Register an account or log in to begin using Memory Quest.

   
Database storage: The application supports persistent database storage through Google Drive. Before launching the application, the optional Drive cell mounts Google Drive and sets the `MQ_DB` environment variable to `/content/drive/MyDrive/memory_quest.db`. User accounts and progress are stored in this database and can persist across Colab runtime resets, provided the database remains in Drive and the Drive setup is completed before launching the application.



Option 2: Run in Google Colab

Steps

1. Open Google Colab.

2. Upload `memory_quest_colab.ipynb`.

3. Run the cells in order.

4. Run the optional Google Drive cell and authorize access when prompted.

5. Run the final application cell.

6. Access Memory Quest through the interface displayed below the final cell.

Database persistence: The optional Google Drive integration stores the SQLite database at `MyDrive/memory_quest.db`. This allows user accounts and progress to persist between Colab sessions, provided the database is correctly configured and Google Drive remains accessible.

Database storage: By default, the database is stored on the Colab runtime's local disk. It is lost when the runtime resets. The optional Google Drive integration allows the database to persist, provided the Drive setup and database configuration are executed correctly before the application starts.




Adaptive Difficulty Logic

Unscramble Practice: The application uses rolling accuracy to adjust the difficulty level.



[Rolling accuracy: Action]

Above 95%: Increase difficulty

From 75% through 95%: Maintain difficulty

Below 75%: Decrease difficulty


The thresholds centre the adjustment strategy around a target accuracy of approximately 85% [13].




Word Chain Auto Mode [Performance: Action]

Two consecutive wins: Increase difficulty

Two consecutive losses: Decrease difficulty

Otherwise: Maintain difficulty




Testing and Validation:

Memory Quest was tested through its Gradio interface in a headless browser using Gradio 5.50 and was also run in Google Colab.

The test run covered:

- Baseline testing.

- Unscramble practice.

- Re-testing and baseline comparison.

- Word Chain wins and losses.

- Daily quests and recall challenges.

- Spaced repetition and review.

- Dashboard data and progress tracking.


The browser test covered the timed sequence display, hiding, and scrambling behaviour. No JavaScript errors were observed during that browser run.

Google Drive integration was used to configure persistent SQLite database storage. The persistence of user data across runtime resets was verified separately only if explicitly tested.




Testing limitations:

- The application was tested in Google Colab; Visual Studio Code execution has not been verified unless separately tested.

- Gradio 5.50 was used for browser testing. Other versions have not been fully validated.

- Testing covered the features listed above but does not guarantee that every possible edge case is free of defects.




Dependencies and Compatibility:

Install dependencies using requirements.txt.

The project pins Gradio to the 5.x series because Gradio 6 changed some Blocks options used by the application. Use a 
compatible Gradio 5.x release, preferably the version tested for this build.

For the most reproducible environment, use a clean Python virtual environment before installing the dependencies.




Important Implementation Notes:

•	Timed display: The sequence show-and-hide timing is controlled on the server. On a slow Colab connection, the sequence may remain visible slightly longer than the configured duration.

•	Database persistence: Local VS Code execution retains the database on disk. Colab runtime storage is temporary unless Google Drive persistence is configured.

•	Spaced repetition: The half-life scheduler is a simplified implementation, not a validated or fitted memory model.

•	Security: Passwords are hashed with PBKDF2. This protects stored passwords, but does not by itself provide every security control required for a production authentication system.




References:

The adaptive difficulty thresholds and simplified half-life approach follow the references numbered [13] and [6], respectively, in the project's analysis materials. Those specific materials are listed below:

   [6] Settles, B., & Meeder, B. (2016). *A Trainable Spaced Repetition Model for Language Learning.* In Proceedings of the 54th Annual Meeting of the Association for Computational Linguistics (Volume 1: Long Papers), 1848–1858. https://doi.org/10.18653/v1/P16-1174
   
   [13] Wilson, R. C., Shenhav, A., Straccia, M., & Cohen, J. D. (2019). *The Eighty Five Percent Rule for optimal learning.* Nature Communications, 10, Article 4646. https://doi.org/10.1038/s41467-019-12552-4




Conclusion:

Memory Quest combines memory games, adaptive difficulty, daily quests, spaced repetition, and performance analytics in a single Python application. It supports local execution through VS Code and notebook-based execution through Google Colab, with SQLite used to store user progress.
