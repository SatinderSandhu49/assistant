from flask import render_template, url_for, redirect, abort, request
from bson.objectid import ObjectId
import random
from flask import Flask, Response
from twilio.twiml.voice_response import VoiceResponse, Gather
import openai
import os
import re
from collections import defaultdict
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
import datetime
import logging
import threading
from twilio.request_validator import RequestValidator
from functools import wraps

# Set up logging
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Replace with your OpenAI API key or set it as an environment variable
openai.api_key = os.getenv('OPENAI_API_KEY')

# Initialize MongoDB client
try:
    uri = os.getenv('MONGO_URI')  # Use MongoDB connection URI from environment variable
    mongo_client = MongoClient(uri, server_api=ServerApi('1'), tls=True, tlsAllowInvalidCertificates=False)  # Secure SSL/TLS
    mongo_client.admin.command('ping')  # Send a ping to confirm a successful connection
    mongo_db = mongo_client.get_database('chatbot')
    mongo_collection = mongo_db['conversations']
    logging.info("Successfully connected to MongoDB")
except Exception as e:
    logging.error(f"Failed to connect to MongoDB: {e}")
    raise

# Initialize conversation histories and no_input_counter for all users
conversation_histories = defaultdict(lambda: {'history': [], 'no_input_counter': 0})

# Twilio Request Validator
account_sid = os.getenv('TWILIO_ACCOUNT_SID')
auth_token = os.getenv('TWILIO_AUTH_TOKEN')
twilio_validator = RequestValidator(auth_token)

def validate_twilio_request(func):
    @wraps(func)
    def decorated_function(*args, **kwargs):
        # Skip validation for local requests
        if request.remote_addr == '127.0.0.1':
            return func(*args, **kwargs)
        
        signature = request.headers.get('X-Twilio-Signature', '')
        url = request.url
        params = request.form
        if not twilio_validator.validate(url, params, signature):
            logging.warning(f"Unauthorized request from IP: {request.remote_addr}")
            abort(403)
        return func(*args, **kwargs)
    return decorated_function

@app.route('/voice', methods=['GET', 'POST'])
@validate_twilio_request  # Only validate Twilio requests, remove API key requirement
def voice():
    """Respond to incoming calls with a voice prompt."""
    resp = VoiceResponse()
    from_number = request.values.get('From')

    # Reset no_input_counter for new calls
    user_data = conversation_histories[from_number]
    user_data['no_input_counter'] = 0

    # Greet the user and start the conversation
    gather = Gather(
        input='speech',
        action='/process',
        method='POST',
        timeout=5,
        speech_timeout='auto',
        language='en-US'
    )
    gather.say('Hello! How can I assist you today?', voice='alice', language='en-US', bargeIn=True)
    resp.append(gather)

    return Response(str(resp), mimetype='application/xml')

@app.route('/process', methods=['GET', 'POST'])
@validate_twilio_request  # Only validate Twilio requests, remove API key requirement
def process():
    """Process the user's speech input and keep the conversation ongoing."""
    resp = VoiceResponse()
    from_number = request.values.get('From')
    user_input = request.values.get('SpeechResult')

    # Get the conversation history and no_input_counter for this user
    user_data = conversation_histories[from_number]
    conversation_history = user_data['history']
    no_input_counter = user_data.get('no_input_counter', 0)

    if user_input:
        # Reset no_input_counter since we received input
        user_data['no_input_counter'] = 0

        # Append sanitized user input to conversation history
        sanitized_input = re.sub(r'[^a-zA-Z0-9\s]', '', user_input)  # Allow only alphanumeric and spaces
        conversation_history.append({"role": "user", "content": sanitized_input})

        try:
            # Send the conversation to OpenAI ChatCompletion API
            response = openai.ChatCompletion.create(
                model='gpt-3.5-turbo',
                messages=conversation_history,
                temperature=0.3,
                max_tokens=100,
            )

            ai_response = response['choices'][0]['message']['content'].strip()

            # Append AI response to conversation history
            conversation_history.append({"role": "assistant", "content": ai_response})

            # Respond back to the user with barge-in enabled
            gather = Gather(
                input='speech',
                action='/process',
                method='POST',
                timeout=5,  # Increased timeout
                speech_timeout='auto',
                language='en-US'
            )
            # Use a voice that supports barge-in
            gather.say(ai_response, voice='alice', language='en-US', bargeIn=True)
            resp.append(gather)

        except openai.error.OpenAIError as e:
            logging.error(f"OpenAI API error: {e}")
            resp.say("I'm sorry, I'm having trouble processing your request right now.", voice='alice', language='en-US', bargeIn=True)

            # Create a new gather to continue the conversation
            gather = Gather(
                input='speech',
                action='/process',
                method='POST',
                timeout=5,
                speech_timeout='auto',
                language='en-US'
            )
            resp.append(gather)
    else:
        # Handle no input
        no_input_counter += 1
        user_data['no_input_counter'] = no_input_counter

        if no_input_counter >= 2:
            resp.say("It seems we're having trouble connecting. Please call back later.", voice='alice', language='en-US')
            resp.hangup()
            # Clean up user data
            del conversation_histories[from_number]
        else:
            resp.say("I'm sorry, I didn't catch that. Could you please repeat?", voice='alice', language='en-US', bargeIn=True)
            # Create a new gather to continue the conversation
            gather = Gather(
                input='speech',
                action='/process',
                method='POST',
                timeout=5,
                speech_timeout='auto',
                language='en-US'
            )
            resp.append(gather)

    # Save the conversation history to MongoDB asynchronously
    def save_to_mongo():
        try:
            existing_conversation = mongo_collection.find_one({"from_number": from_number})
            if existing_conversation:
                # Update the existing conversation
                mongo_collection.update_one(
                    {"from_number": from_number},
                    {"$set": {"conversation": conversation_history, "timestamp": datetime.datetime.now(datetime.UTC)}}
                )
            else:
                # Insert a new conversation
                mongo_collection.insert_one({
                    "from_number": from_number,
                    "conversation": conversation_history,
                    "timestamp": datetime.datetime.now(datetime.UTC)
                })
            logging.info(f"Conversation history successfully saved for {from_number}")
        except Exception as e:
            logging.error(f"Failed to save conversation to MongoDB: {e}")

    threading.Thread(target=save_to_mongo).start()

    return Response(str(resp), mimetype='application/xml')


@app.route('/dashboard', methods=['GET'])
def dashboard():
    # Fetch data from MongoDB
    total_calls = mongo_collection.count_documents({})
    total_duration = random.randint(100, 1000)  # Random value for total duration
    num_cities = random.randint(1, 10)  # Random value for number of cities

    # Fetch list of calls
    calls = mongo_collection.find().sort('timestamp', -1)
    call_list = []
    for call in calls:
        call_data = {
            'from_number': call['from_number'],
            'timestamp': call['timestamp'],
            '_id': str(call['_id'])
        }
        call_list.append(call_data)

    # Random user details
    user_details = {
        'name': 'John Doe',
        'number': '+1 234 567 8900',
        'company': 'Example Corp'
    }

    return render_template('dashboard.html', user_details=user_details,
                           total_calls=total_calls, total_duration=total_duration,
                           num_cities=num_cities, call_list=call_list)

@app.route('/conversation/<conversation_id>', methods=['GET'])
def conversation_details(conversation_id):
    # Fetch conversation from MongoDB
    conversation = mongo_collection.find_one({'_id': ObjectId(conversation_id)})

    if not conversation:
        return "Conversation not found", 404

    return render_template('conversation.html', conversation=conversation)


if __name__ == '__main__':
    app.run(debug=True)
