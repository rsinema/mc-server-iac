import json
import boto3
import os
import logging

# Set up logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    logger.info(f"Event received: {json.dumps(event)}")
    
    # Define CORS headers
    cors_headers = {
        'Access-Control-Allow-Origin': '*',  # Use your specific domain in production
        'Access-Control-Allow-Headers': 'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token',
        'Access-Control-Allow-Methods': 'GET,OPTIONS,POST'
    }
    
    # Handle preflight OPTIONS request
    if event.get('httpMethod') == 'OPTIONS':
        logger.info("Handling OPTIONS preflight request")
        return {
            'statusCode': 200,
            'headers': cors_headers,
            'body': json.dumps({'message': 'CORS preflight successful'})
        }
    
    # Extract action from the event - checking multiple formats
    action = None
    
    # First check if this is a new REST endpoint call
    if 'path' in event:
        path = event.get('path', '')
        logger.info(f"Found path in event: {path}")
        
        # Extract action from path
        if path.endswith('/status'):
            action = 'status'
            logger.info("Extracted 'status' action from path")
        elif path.endswith('/start'):
            action = 'start'
            logger.info("Extracted 'start' action from path")
        elif path.endswith('/stop'):
            action = 'stop'
            logger.info("Extracted 'stop' action from path")
    
    # If action wasn't found in path, check other locations (for backward compatibility)
    if not action:
        # Check if action is directly in the event (direct invocation format)
        if 'action' in event:
            action = event.get('action', '').lower()
            logger.info(f"Found action directly in event: {action}")
        
        # If not, check in the body (API Gateway format)
        elif 'body' in event and event['body']:
            try:
                logger.info(f"Checking for action in body: {event['body']}")
                body = json.loads(event['body'])
                action = body.get('action', '').lower()
                logger.info(f"Extracted action from body: {action}")
            except Exception as e:
                logger.error(f"Error parsing body JSON: {str(e)}")
                logger.error(f"Raw body content: {event['body']}")
                
        # If action is not found yet, check queryStringParameters
        if not action and 'queryStringParameters' in event and event['queryStringParameters']:
            logger.info(f"Checking queryStringParameters: {event['queryStringParameters']}")
            action = event['queryStringParameters'].get('action', '').lower()
            if action:
                logger.info(f"Found action in queryStringParameters: {action}")
            
        # If still no action, check pathParameters
        if not action and 'pathParameters' in event and event['pathParameters']:
            logger.info(f"Checking pathParameters: {event['pathParameters']}")
            action = event['pathParameters'].get('action', '').lower()
            if action:
                logger.info(f"Found action in pathParameters: {action}")
    
    # Get the instance ID from environment variables
    instance_id = os.environ.get('INSTANCE_ID')
    logger.info(f"Instance ID from environment: {instance_id}")
    
    # If no instance ID is set, return an error
    if not instance_id:
        logger.error("No INSTANCE_ID environment variable set")
        return {
            'statusCode': 400,
            'headers': cors_headers,
            'body': json.dumps({
                'message': 'No instance ID configured',
                'error': 'INSTANCE_ID environment variable not set'
            })
        }
    
    # If no action was found, return an error
    if not action:
        logger.error("No action specified in the request")
        return {
            'statusCode': 400,
            'headers': cors_headers,
            'body': json.dumps({
                'message': 'No action specified',
                'error': 'Please specify an action: start, stop, or status',
                'event': event  # Include the event for debugging
            })
        }
    
    # Initialize the EC2 client
    logger.info("Initializing EC2 client")
    ec2 = boto3.client('ec2')
    
    # Get the current state of the instance
    try:
        logger.info(f"Getting current state for instance {instance_id}")
        response = ec2.describe_instances(InstanceIds=[instance_id])
        current_state = response['Reservations'][0]['Instances'][0]['State']['Name']
        logger.info(f"Current instance state: {current_state}")
    except Exception as e:
        logger.error(f"Error getting instance state: {str(e)}")
        return {
            'statusCode': 400,
            'headers': cors_headers,
            'body': json.dumps({
                'message': f'Error getting instance state: {str(e)}',
                'instance_id': instance_id
            })
        }
    
    # Prepare the result dictionary
    result = {
        'instance_id': instance_id,
        'previous_state': current_state
    }
    
    # Process the action
    logger.info(f"Processing action: {action}")
    
    if action == 'start':
        if current_state == 'stopped':
            logger.info(f"Starting instance {instance_id}")
            ec2.start_instances(InstanceIds=[instance_id])
            result['message'] = 'Server is starting'
            result['action_taken'] = 'start'
        else:
            logger.info(f"No action taken. Instance is already in {current_state} state")
            result['message'] = f'Server is already in {current_state} state'
            result['action_taken'] = 'none'
    
    elif action == 'stop':
        if current_state == 'running':
            logger.info(f"Stopping instance {instance_id}")
            ec2.stop_instances(InstanceIds=[instance_id])
            result['message'] = 'Server is stopping'
            result['action_taken'] = 'stop'
        else:
            logger.info(f"No action taken. Instance is already in {current_state} state")
            result['message'] = f'Server is already in {current_state} state'
            result['action_taken'] = 'none'
    
    elif action == 'status':
        logger.info(f"Status check: instance {instance_id} is {current_state}")
        result['message'] = f'Server is in {current_state} state'
        result['action_taken'] = 'status_check'
    
    else:
        logger.error(f"Invalid action received: {action}")
        return {
            'statusCode': 400,
            'headers': cors_headers,
            'body': json.dumps({
                'message': 'Invalid action. Use "start", "stop", or "status".',
                'received_action': action
            })
        }
    
    # Return the response with CORS headers
    logger.info(f"Returning result: {json.dumps(result)}")
    return {
        'statusCode': 200,
        'headers': cors_headers,
        'body': json.dumps(result)
    }