pipeline {
    agent any

    environment {
        IMAGE_NAME = 'video-downloader'
        CONTAINER_NAME = 'video-downloader'
    }

    stages {
        stage('Build') {
            steps {
                sh "docker build -t ${IMAGE_NAME}:latest ."
            }
        }

        stage('Deploy') {
            steps {
                sh """
                    docker stop ${CONTAINER_NAME} || true
                    docker rm ${CONTAINER_NAME} || true
                    docker compose up -d
                """
            }
        }
    }

    post {
        failure {
            echo 'Build or deploy failed!'
        }
        success {
            echo 'Deployed successfully!'
        }
    }
}
